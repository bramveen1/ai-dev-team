"""Router app — multi-agent slack_bolt application.

Constructs one ``AsyncApp`` + ``AsyncSocketModeHandler`` per agent that has
configured Slack credentials. Events are dispatched to the agent whose Bolt
app received them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from router.approvals.capabilities_loader import get_capability_instance
from router.approvals.handlers import register_handlers as register_approval_handlers
from router.approvals.interceptor import parse_response, post_approval_message
from router.approvals.store import DraftStore
from router.config import get_agent_map, load_config
from router.dispatcher import dispatch
from router.memory_curator import curate_agent_memory, needs_curation
from router.mentions import last_mentioned
from router.packs.grants import maybe_handle_pack_command, resolve_pending_reply
from router.scheduled_tasks.bootstrap import setup_scheduled_tasks
from router.session_end import handle_clean_exit, handle_timeout_exit, is_exit_trigger
from router.session_manager import (
    add_to_thread_history,
    create_session,
    find_session_by_thread,
    pop_timed_out_sessions,
    update_activity,
)
from router.slack_format import md_to_slack
from router.threads.state import get_default_store

load_dotenv()

config = load_config()

logging.basicConfig(
    level=getattr(logging, config["log_level"], logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_bolt_logger = logging.getLogger("slack_bolt")
_bolt_logger.setLevel(logging.INFO)

# --- Approval flow setup ---
_draft_store = DraftStore()

# Per-agent state, populated by _build_apps() at module load and by main()
# at startup. Each dict is keyed by agent name. Socket Mode handlers are
# constructed lazily in main() because their aiohttp client session needs
# a running event loop.
_apps_by_agent: dict[str, AsyncApp] = {}
_app_tokens_by_agent: dict[str, str] = {}
_bot_user_id_by_agent: dict[str, str] = {}

# Bot user ID → agent name reverse map. Populated at startup from the auth.test
# call on each app. Used by mention parsing in agent-handoff detection.
_bot_user_map: dict[str, str] = {}


DEFAULT_THINKING_STATUS = "is thinking…"


def _build_apps() -> None:
    """Construct one ``AsyncApp`` per agent with configured Slack credentials.

    Each app is registered with the same approval/event/scheduled-task handlers,
    bound to the agent name via closure so the receiving agent is known.
    """
    _apps_by_agent.clear()
    _app_tokens_by_agent.clear()

    slack_credentials = config.get("slack_credentials", {})
    if not slack_credentials:
        logger.warning("No Slack credentials configured for any agent; router will not connect to Slack")
        return

    for agent_name, creds in slack_credentials.items():
        bolt_app = AsyncApp(
            token=creds["bot_token"],
            signing_secret=creds["signing_secret"],
            logger=_bolt_logger,
        )
        register_approval_handlers(bolt_app, _draft_store)

        on_app_mention, on_message = _make_event_handlers(agent_name)
        bolt_app.event("app_mention")(on_app_mention)
        bolt_app.event("message")(on_message)

        _apps_by_agent[agent_name] = bolt_app
        _app_tokens_by_agent[agent_name] = creds["app_token"]
        logger.info("Built Bolt app for agent=%s", agent_name)


def _make_event_handlers(agent_name: str):
    """Build per-agent app_mention / message handlers.

    Slack Bolt inspects handler signatures and only injects arguments it
    recognizes (event, say, client, body, ...). Capturing ``agent_name``
    via a factory closure (rather than a default arg) keeps the Bolt-facing
    signature clean — otherwise Bolt logs "<arg> is not a valid argument"
    and leaves the parameter unbound.
    """

    async def on_app_mention(event, say, client):
        await _handle_event(event, say, client, receiving_agent=agent_name, was_mentioned=True)

    async def on_message(event, say, client):
        await handle_message(event, say, client, receiving_agent=agent_name)

    return on_app_mention, on_message


_build_apps()


def _client_for_agent(agent_name: str) -> Any | None:
    """Return the Slack WebClient for ``agent_name``, or None if not configured."""
    bolt_app = _apps_by_agent.get(agent_name)
    return bolt_app.client if bolt_app else None


async def set_assistant_status(client, channel: str, thread_ts: str, status: str) -> None:
    """Set the assistant thread status indicator (auto-clears on next message).

    Uses the Slack assistant.threads.setStatus API which renders as
    "<App Name> <status>" beneath the bot's name in the thread.
    The status auto-clears when the bot posts a message or after 2 minutes.
    """
    try:
        await client.assistant_threads_setStatus(
            channel_id=channel,
            thread_ts=thread_ts,
            status=status,
        )
    except Exception:
        logger.debug("Could not set assistant status (non-critical)")


async def _handle_event(event: dict, say, client, receiving_agent: str, was_mentioned: bool) -> None:
    """Handle a Slack event for a specific receiving agent.

    Args:
        event: The Slack event dict.
        say: Bolt ``say`` helper, scoped to the receiving agent's app.
        client: Bolt ``client`` (Slack WebClient), scoped to the receiving agent.
        receiving_agent: Name of the agent whose Bolt app received this event.
        was_mentioned: True if the user explicitly @-mentioned this agent in
            this event (i.e. this came in via ``app_mention``).
    """
    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "") or ""
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    event_type = event.get("type", "unknown")

    logger.info(
        "Received event type=%s agent=%s channel=%s user=%s thread_ts=%s text=%s",
        event_type,
        receiving_agent,
        channel,
        user,
        thread_ts,
        text[:80] if text else "",
    )

    # Ignore bot messages to avoid loops
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        logger.debug("Ignoring bot message")
        return

    # If a pack's authenticate.py is awaiting the user's next reply in this
    # thread (e.g. "paste your token"), deliver it and stop here — the grant
    # flow will continue on its own.
    if channel and thread_ts and resolve_pending_reply(channel, thread_ts, text):
        return

    # Pack provisioning commands (grant / revoke / list packs / who has) are
    # handled inline by the router rather than dispatched to the agent CLI.
    # Any agent's app can receive them — the target agent is named in the
    # command text. We wrap `say` so every response stays in the same thread
    # as the original command — without that, follow-up replies (e.g. PAT
    # paste) lose their parent and the grant flow can't correlate them.
    async def _threaded_say(reply: str) -> None:
        if thread_ts:
            await say(text=reply, thread_ts=thread_ts)
        else:
            await say(text=reply)

    try:
        if await maybe_handle_pack_command(text, _threaded_say, channel=channel, thread_ts=thread_ts):
            return
    except Exception:
        logger.exception("Error handling pack command (text=%s)", text[:80])
        return

    agent_name = receiving_agent
    agent_map = get_agent_map()
    if agent_name not in agent_map:
        logger.error("Agent %s not found in agent map", agent_name)
        return

    # Record authoritative active agent for this thread. Mentions bump
    # last_mention_at; un-mentioned follow-ups just refresh updated_at.
    if channel and thread_ts:
        try:
            get_default_store().set_active_agent(
                channel_id=channel,
                thread_ts=thread_ts,
                agent_name=agent_name,
                mentioned=was_mentioned,
            )
        except Exception:
            logger.exception("Failed to update thread state")

    # Find existing session for this agent+thread or create a new one. When
    # a thread is handed off to a different agent, each agent gets its own
    # session so memory writes and activity timers stay isolated.
    session = find_session_by_thread(channel, thread_ts, agent_name=agent_name)
    if session is None:
        session = create_session(channel=channel, thread_ts=thread_ts, agent_name=agent_name)
        logger.debug("Created session %s for agent=%s", session["session_id"], agent_name)
    else:
        update_activity(session["session_id"])
        logger.debug("Reusing session %s for agent=%s", session["session_id"], agent_name)

    # Check for clean exit trigger
    if is_exit_trigger(text):
        logger.info("Exit trigger detected in thread=%s from user=%s", thread_ts, user)
        agent_config = agent_map[agent_name]
        try:
            await handle_clean_exit(
                agent_name=agent_name,
                container=agent_config["container"],
                thread_history=[],  # Thread history loading is added in #11
                slack_client=client,
                channel=channel,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Error during clean exit for agent %s", agent_name)
        await say(text="You're welcome! I've saved our conversation notes.", thread_ts=thread_ts)
        return

    # Trigger background memory curation if needed (first message of the day)
    agent_config = agent_map[agent_name]
    if needs_curation(agent_name):
        logger.info("Triggering background memory curation for %s", agent_name)
        asyncio.create_task(curate_agent_memory(agent_name, agent_config["container"]))

    # Show assistant status indicator while the agent works
    thinking_text = agent_config.get("thinking_status", DEFAULT_THINKING_STATUS)
    await set_assistant_status(client, channel, thread_ts, thinking_text)

    # Record the user's message in session history
    add_to_thread_history(session["session_id"], {"user": user, "text": text})

    # Dispatch to agent
    try:
        result = await dispatch(
            agent_name=agent_name,
            message=text,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config["session_timeout"],
            max_token_budget=config["max_token_budget"],
            bot_user_map=dict(_bot_user_map),
        )

        update_activity(session["session_id"])

        # Check for draft-approval blocks in the response
        intercept = parse_response(result["response"])

        # Record the agent's response in session history (use cleaned text)
        response_text = intercept.cleaned_text if intercept.has_drafts else result["response"]
        add_to_thread_history(session["session_id"], {"user": agent_name, "text": response_text})

        # Post the agent's text response (cleaned of approval blocks)
        if response_text:
            await say(text=md_to_slack(response_text), thread_ts=thread_ts)

        # Post approval messages for any draft-approval blocks
        for draft_req in intercept.draft_requests:
            cap_instance = get_capability_instance(
                agent_name=agent_name,
                capability_type=draft_req.capability_type,
                instance_name=draft_req.capability_instance,
            )
            try:
                await post_approval_message(
                    draft_request=draft_req,
                    agent_name=agent_name,
                    channel=channel,
                    thread_ts=thread_ts,
                    client=client,
                    store=_draft_store,
                    capability_instance=cap_instance,
                )
            except Exception:
                logger.exception("Failed to post approval message for draft %s", draft_req.draft_id)

        logger.info("Responded in thread=%s agent=%s", thread_ts, agent_name)

        # Agent-initiated handoff: if the agent's response @mentions another
        # known agent, promote that agent to "active" so the next message in
        # this thread is dispatched to them (unless the next message mentions
        # someone else, which always wins).
        _maybe_handle_agent_handoff(
            response_text=result["response"],
            current_agent=agent_name,
            channel=channel,
            thread_ts=thread_ts,
        )

    except Exception:
        logger.exception("Error dispatching to agent %s", agent_name)
        await say(text="Sorry, something went wrong while processing your request.", thread_ts=thread_ts)


def _maybe_handle_agent_handoff(
    response_text: str,
    current_agent: str,
    channel: str,
    thread_ts: str,
) -> None:
    """If ``response_text`` @mentions another agent, update thread state.

    Agent responses can request handoffs by @-mentioning another agent in
    their reply (e.g. "I'll loop in @dave on this"). When the mentioned
    agent is someone *other* than the current agent, we set them as the
    active agent so the next un-mentioned follow-up goes to them.
    """
    if not channel or not thread_ts or not response_text:
        return

    agent_names = list(get_agent_map().keys())
    mentioned = last_mentioned(response_text, agent_names, _bot_user_map)
    if not mentioned or mentioned == current_agent:
        return

    try:
        get_default_store().set_active_agent(
            channel_id=channel,
            thread_ts=thread_ts,
            agent_name=mentioned,
            mentioned=True,
        )
        logger.info(
            "Agent handoff detected: %s -> %s in thread=%s",
            current_agent,
            mentioned,
            thread_ts,
        )
    except Exception:
        logger.exception("Failed to record agent-initiated handoff")


async def handle_app_mention(event, say, client, receiving_agent: str) -> None:
    """Handle @mentions of the bot in channels for ``receiving_agent``."""
    await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=True)


async def handle_message(event, say, client, receiving_agent: str) -> None:
    """Handle DMs and thread follow-ups for ``receiving_agent``.

    Dedup rules with multiple per-agent apps installed in a workspace:

    * DMs are scoped to a single bot, so always handle.
    * Channel messages mentioning *any* known bot are deferred to the
      mentioned agent's ``app_mention`` handler — skip here.
    * Channel thread replies with no mention are handled only when this
      agent is the thread's active agent. The active agent flag arbitrates
      between multiple agents that may have sessions in the same thread.
    """
    channel_type = event.get("channel_type", "")
    text = event.get("text", "") or ""

    # DM to this agent's bot — always handle
    if channel_type == "im":
        await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=False)
        return

    # Channel message that @-mentions any known bot user → let the mentioned
    # agent's app_mention handler deal with it.
    if any(f"<@{uid}>" in text for uid in _bot_user_id_by_agent.values()):
        return

    # Channel thread reply with no mention — handle iff this agent is active
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event.get("channel", "")
    active_agent: str | None = None
    try:
        active_agent = get_default_store().get_active_agent(channel, thread_ts)
    except Exception:
        logger.exception("Failed to read thread state")

    if active_agent != receiving_agent:
        return

    await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=False)


async def _session_cleanup_loop(interval_seconds: int = 60) -> None:
    """Periodically clean up timed-out sessions and post summaries."""
    agent_map = get_agent_map()
    logger.info("Session cleanup loop started (interval=%ds)", interval_seconds)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            expired = pop_timed_out_sessions(config["session_timeout"])
            for session in expired:
                agent_name = session["agent_name"]
                agent_config = agent_map.get(agent_name)
                if not agent_config:
                    logger.warning("No agent config for %s, skipping timeout exit", agent_name)
                    continue

                slack_client = _client_for_agent(agent_name)
                if slack_client is None:
                    logger.warning("No Slack client for %s, skipping timeout exit", agent_name)
                    continue

                try:
                    await handle_timeout_exit(
                        agent_name=agent_name,
                        container=agent_config["container"],
                        thread_history=session.get("thread_history", []),
                        slack_client=slack_client,
                        channel=session["channel"],
                        thread_ts=session["thread_ts"],
                    )
                except Exception:
                    logger.exception("Error during timeout exit for session %s", session["session_id"])

            if expired:
                logger.info("Cleaned up %d timed-out sessions", len(expired))
        except Exception:
            logger.exception("Error during session cleanup")


async def _expiration_worker_loop(interval_seconds: int = 3600) -> None:
    """Periodically run the draft expiration worker."""
    from router.approvals.expiration_worker import run_once

    logger.info("Draft expiration worker started (interval=%ds)", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            counts = await run_once(store=_draft_store, client_resolver=_client_for_agent)
            total = sum(counts.values())
            if total:
                logger.info("Expiration worker: %s", counts)
        except Exception:
            logger.exception("Error in expiration worker")


async def main():
    """Start the router: run one Socket Mode handler per configured agent."""
    logger.info("Starting router service for %d agent(s)...", len(_apps_by_agent))

    # Resolve each agent's bot user ID via auth.test, populate the reverse map.
    for agent_name, bolt_app in _apps_by_agent.items():
        try:
            auth_resp = await bolt_app.client.auth_test()
            bot_user_id = auth_resp["user_id"]
            _bot_user_id_by_agent[agent_name] = bot_user_id
            _bot_user_map[bot_user_id] = agent_name
            logger.info("Bot user ID for agent=%s: %s", agent_name, bot_user_id)
        except Exception:
            logger.warning("Could not resolve bot user ID for agent=%s via auth.test", agent_name)

    asyncio.create_task(_session_cleanup_loop())
    asyncio.create_task(_expiration_worker_loop())

    # Register scheduled-task handlers and the scheduler loop on each agent's
    # Bolt app. Slack scopes slash command ownership workspace-wide, so each
    # agent's app must register a unique command name (``/<prefix><name>-tasks``).
    # ``SLASH_COMMAND_PREFIX`` lets a dev deployment (e.g. dev- prefix) coexist
    # with prod in the same workspace.
    slash_prefix = os.environ.get("SLASH_COMMAND_PREFIX", "")
    for agent_name, bolt_app in _apps_by_agent.items():

        def _resolve_agent_for_command(_body: dict, _agent: str = agent_name) -> str:
            return _agent

        command_name = f"/{slash_prefix}{agent_name}-tasks"
        setup_scheduled_tasks(
            bolt_app=bolt_app,
            slack_client=bolt_app.client,
            dispatch_fn=dispatch,
            agent_resolver=_resolve_agent_for_command,
            command_name=command_name,
        )
        logger.info("Registered slash command %s for agent=%s", command_name, agent_name)

    if not _app_tokens_by_agent:
        logger.error("No agents have Slack credentials; nothing to start")
        return

    # Build Socket Mode handlers now that the event loop is running, then
    # start them all concurrently.
    handlers = [
        AsyncSocketModeHandler(_apps_by_agent[agent_name], app_token)
        for agent_name, app_token in _app_tokens_by_agent.items()
    ]
    await asyncio.gather(*(handler.start_async() for handler in handlers))


if __name__ == "__main__":
    asyncio.run(main())
