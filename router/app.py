"""Router app — main slack_bolt application for the multi-agent system.

Receives Slack events (app_mention, DM messages) and dispatches them
to the appropriate agent container via the dispatcher module.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from router.approvals.capabilities_loader import get_capability_instance
from router.approvals.handlers import register_handlers
from router.approvals.interceptor import parse_response, post_approval_message
from router.approvals.store import DraftStore
from router.config import get_agent_map, load_config
from router.dispatcher import dispatch
from router.memory_curator import curate_agent_memory, needs_curation
from router.mentions import last_mentioned, resolve_target_agent
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
app = AsyncApp(
    token=config["slack_bot_token"],
    signing_secret=config["slack_signing_secret"],
    logger=_bolt_logger,
)

# --- Approval flow setup ---
_draft_store = DraftStore()
register_handlers(app, _draft_store)

# Bot user ID → agent name mapping, populated at startup
_bot_user_map: dict[str, str] = {}

# Bot's own user ID, populated at startup
_bot_user_id: str | None = None


DEFAULT_AGENT = "lisa"


def _resolve_agent(event: dict) -> tuple[str | None, bool]:
    """Determine which agent should handle this event.

    Resolution order:

    1. Any explicit @mention of a known agent in the message text.
       The last-mentioned agent wins.
    2. The thread's active agent (from thread_state), for unmentioned
       follow-up replies.
    3. Default agent (currently "lisa").

    Args:
        event: The Slack event dict.

    Returns:
        ``(agent_name, was_mentioned)`` — ``was_mentioned`` is True when
        the agent was selected because of an explicit mention, False when
        selected from thread state or default.
    """
    text = event.get("text", "") or ""
    agent_map = get_agent_map()
    agent_names = list(agent_map.keys())

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")

    active_agent: str | None = None
    if channel and thread_ts:
        try:
            active_agent = get_default_store().get_active_agent(channel, thread_ts)
        except Exception:
            logger.exception("Failed to read thread state; falling back to default")

    return resolve_target_agent(
        text=text,
        agent_names=agent_names,
        bot_user_map=_bot_user_map,
        active_agent=active_agent,
        default_agent=DEFAULT_AGENT,
    )


DEFAULT_THINKING_STATUS = "is thinking\u2026"


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


async def _handle_event(event: dict, say, client) -> None:
    """Common handler for app_mention and message events."""
    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    event_type = event.get("type", "unknown")

    logger.info(
        "Received event type=%s channel=%s user=%s thread_ts=%s text=%s",
        event_type,
        channel,
        user,
        thread_ts,
        text[:80] if text else "",
    )

    # Ignore bot messages to avoid loops
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        logger.debug("Ignoring bot message")
        return

    resolved = _resolve_agent(event)
    # Back-compat: older tests may patch _resolve_agent to return just a name.
    if isinstance(resolved, tuple):
        agent_name, was_mentioned = resolved
    else:
        agent_name, was_mentioned = resolved, False

    if agent_name is None:
        logger.warning("Could not resolve agent for event in channel=%s", channel)
        return

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


@app.event("app_mention")
async def handle_app_mention(event, say, client):
    """Handle @mentions of the bot in channels."""
    await _handle_event(event, say, client)


@app.event("message")
async def handle_message(event, say, client):
    """Handle direct messages and thread follow-ups to the bot."""
    channel_type = event.get("channel_type", "")

    # Always handle DMs
    if channel_type == "im":
        await _handle_event(event, say, client)
        return

    # In channels, handle thread replies where the bot has an active session
    # Skip messages that @mention the bot — those are already handled by app_mention
    if _bot_user_id and f"<@{_bot_user_id}>" in event.get("text", ""):
        return

    thread_ts = event.get("thread_ts")
    if thread_ts:
        channel = event.get("channel", "")
        session = find_session_by_thread(channel, thread_ts)
        if session:
            await _handle_event(event, say, client)
            return


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

                try:
                    await handle_timeout_exit(
                        agent_name=agent_name,
                        container=agent_config["container"],
                        thread_history=session.get("thread_history", []),
                        slack_client=app.client,
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
            counts = await run_once(store=_draft_store, client=app.client)
            total = sum(counts.values())
            if total:
                logger.info("Expiration worker: %s", counts)
        except Exception:
            logger.exception("Error in expiration worker")


async def main():
    """Start the router in Socket Mode."""
    global _bot_user_id
    logger.info("Starting router service...")

    # Resolve the bot's own user ID so we can deduplicate events
    try:
        auth_resp = await app.client.auth_test()
        _bot_user_id = auth_resp["user_id"]
        logger.info("Bot user ID: %s", _bot_user_id)
    except Exception:
        logger.warning("Could not resolve bot user ID via auth.test")

    asyncio.create_task(_session_cleanup_loop())
    asyncio.create_task(_expiration_worker_loop())

    def _resolve_agent_for_command(body: dict) -> str | None:
        # /tasks is scoped to the agent whose bot received the command.
        # Until per-agent bot tokens land, default to the Phase 1 agent.
        return "lisa"

    setup_scheduled_tasks(
        bolt_app=app,
        slack_client=app.client,
        dispatch_fn=dispatch,
        agent_resolver=_resolve_agent_for_command,
    )

    handler = AsyncSocketModeHandler(app, config["slack_app_token"])
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
