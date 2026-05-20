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

from router.approvals.handlers import register_handlers as register_approval_handlers
from router.approvals.interceptor import parse_response, post_approval_message
from router.approvals.store import Draft, DraftStore
from router.config import get_agent_map, load_config
from router.dispatch import state as _dstate
from router.dispatch.discovery import start_discovery_loop
from router.dispatcher import dispatch
from router.healthz import mark_ready
from router.healthz import start_server as start_healthz_server
from router.kill_command import register_kill_handler
from router.memory_curator import curate_agent_memory, needs_curation
from router.mentions import last_mentioned
from router.packs.grants import maybe_handle_pack_command, resolve_pending_reply
from router.scheduled_tasks.bootstrap import (
    open_store,
    setup_scheduled_tasks_handlers,
    start_scheduled_tasks_scheduler,
)
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

# Strong references to long-lived background tasks. asyncio only keeps weak
# refs to tasks, so a `create_task(...)` whose return value is discarded can
# be garbage collected mid-flight — silently killing the scheduler loop and
# any other "fire and forget" workers. Anything we want to outlive the call
# stack that started it must be parked here.
_background_tasks: set[asyncio.Task] = set()

# Module-level handle for the /healthz HTTP server. Kept alive for the
# lifetime of the process so the aiohttp ``AppRunner`` isn't GC'd while
# the socket is still listening.
_healthz_runner: Any | None = None


def _spawn_background_task(coro: Any, *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` and keep a strong reference to the resulting task.

    Without this, asyncio's weak-ref bookkeeping can drop a long-running task
    on a GC pass — see ``_background_tasks``.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


DEFAULT_THINKING_STATUS = "is thinking…"


async def _execute_approved_draft(draft: Draft, channel: str, thread_ts: str, client: Any) -> None:
    """Re-dispatch to the owning agent so it actually runs the approved action.

    Wired in as ``execute_callback`` on the approval handlers. The agent
    drafted the action originally — by re-entering its container with the
    approved draft's metadata as the message, it can execute via the same
    pack tools (``gh pr merge``, etc.) it had access to during drafting.

    The agent's response is parsed for further draft blocks (rare) and
    posted back into the same thread, mirroring the regular event path.

    Special case: ``dispatch.dispatch_issue`` approvals skip the agent CLI
    entirely and call ``packs.dispatch.handler.dispatch_issue`` directly
    in-process (with ``_approved=True``). This avoids a 600 s agent CLI
    re-entry and lets poll-mode dispatches return in < 3 s.
    """
    if draft.capability_instance == "dispatch" and draft.action_verb == "dispatch_issue":
        from packs.dispatch import handler as _dispatch_handler

        # draft.payload mirrors the gate_preview dict produced by
        # packs.dispatch.handler._evaluate_approval_gate — its keys are
        # (issue_url, repo, branch_target, model, est_workspace_path,
        # gate_reason, …), NOT the dispatch_issue() kwargs. Splatting
        # it directly raises TypeError (extra kwargs) and also fails to
        # supply the required channel/thread_ts/agent. Map explicitly:
        # forward only the fields dispatch_issue accepts, and pull
        # channel/thread_ts from the approval thread and agent from
        # the draft's owning agent.
        payload = draft.payload or {}
        issue_url = payload.get("issue_url")
        if not issue_url:
            logger.error(
                "Approved dispatch_issue draft %s has no issue_url in payload — cannot execute",
                draft.draft_id,
            )
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved draft `{draft.draft_id}` missing issue_url; nothing executed.",
            )
            return

        handler_kwargs: dict[str, Any] = {
            "issue_url": issue_url,
            "channel": channel,
            "thread_ts": thread_ts,
            "agent": draft.agent_name,
            "_approved": True,
        }
        # Forward optional fields only when the gate preview supplied them.
        # Keep this list aligned with _evaluate_approval_gate's preview shape
        # — adding fields here without adding them to the preview is a no-op,
        # and adding fields to the preview without listing them here means
        # they're silently dropped (preview is for the human, kwargs are for
        # the handler).
        if "model" in payload:
            handler_kwargs["model"] = payload["model"]

        try:
            result: dict[str, Any] = await asyncio.to_thread(lambda: _dispatch_handler.dispatch_issue(**handler_kwargs))
        except Exception:
            logger.exception("Failed to execute dispatch_issue for approved draft %s", draft.draft_id)
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved, but execution failed for draft `{draft.draft_id}`. Check router logs.",
            )
            return

        status = result.get("status")
        dispatch_id = result.get("dispatch_id", draft.draft_id)
        if status == "launched":
            text = f":rocket: dispatch `{dispatch_id}` launched (approved)"
        elif status == "completed":
            text = f":white_check_mark: dispatch `{dispatch_id}` done (exit 0)"
        elif status == "failed":
            exitcode = result.get("exitcode", -1)
            if exitcode == -1:
                text = f":warning: dispatch `{dispatch_id}` terminated (exit -1)"
            else:
                text = f":x: dispatch `{dispatch_id}` failed (exit {exitcode})"
        elif status == "error":
            text = f":x: dispatch `{dispatch_id}` error: {result.get('reason', 'unknown')}"
        else:
            text = f":x: dispatch `{dispatch_id}` unexpected status: {status}"

        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        return

    # All other drafts: agent CLI re-entry path.
    # Build a synthesized prompt the agent will recognize. Keep it tight
    # so the agent doesn't re-draft instead of executing.
    payload_summary = ", ".join(f"{k}={v}" for k, v in (draft.payload or {}).items() if v is not None)
    message = (
        f"The user just approved your earlier draft via the Slack approval card. "
        f"Execute it now. Do not draft again — just run the action and reply with a short confirmation.\n"
        f"\n"
        f"Action: {draft.action_verb}\n"
        f"Pack: {draft.capability_instance}\n"
        f"Payload: {payload_summary or '(none)'}"
    )

    agent_name = draft.agent_name
    agent_map = get_agent_map()
    if agent_name not in agent_map:
        logger.error("Approved draft %s names unknown agent %r — cannot execute", draft.draft_id, agent_name)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: Approved draft references unknown agent `{agent_name}`; nothing executed.",
        )
        return

    try:
        result = await dispatch(
            agent_name=agent_name,
            message=message,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config["session_timeout"],
            bot_user_map=dict(_bot_user_map),
        )
    except Exception:
        logger.exception("Failed to dispatch execution for approved draft %s", draft.draft_id)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: Approved, but execution failed for draft `{draft.draft_id}`. Check router logs.",
        )
        return

    intercept = parse_response(result["response"])
    response_text = intercept.cleaned_text if intercept.has_drafts else result["response"]
    if response_text:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=md_to_slack(response_text),
        )

    # If the agent (mistakenly) re-drafts on the execute step, surface those
    # cards too — at least the user can discard them rather than silent loss.
    for draft_req in intercept.draft_requests:
        try:
            await post_approval_message(
                draft_request=draft_req,
                agent_name=agent_name,
                channel=channel,
                thread_ts=thread_ts,
                client=client,
                store=_draft_store,
            )
        except Exception:
            logger.exception("Failed to post follow-up approval message for draft %s", draft_req.draft_id)


def _resolve_active_agent_for_kill(channel: str, thread_ts: str) -> str | None:
    """Look up the thread's most recently active agent for ``/kill`` fallback.

    When an operator types ``/kill`` with no agent name we kill whoever
    is currently working in this thread. Reads from the thread state
    store the same way the message router does.
    """
    try:
        return get_default_store().get_active_agent(channel, thread_ts)
    except Exception:
        logger.exception("Failed to resolve active agent for /kill fallback")
        return None


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
        register_approval_handlers(bolt_app, _draft_store, execute_callback=_execute_approved_draft)

        # Kill switch — registered on every per-agent app. The handler
        # parses the agent name from the command body, so any agent's
        # bolt_app can carry the ``/kill`` for any other agent (which is
        # what the spec asks for: "/kill sam" must work even when sent
        # to Lisa's app).
        slash_prefix = os.environ.get("SLASH_COMMAND_PREFIX", "")
        kill_command_names = [f"/{slash_prefix}kill"]
        register_kill_handler(
            bolt_app,
            command_name=kill_command_names,
            active_agent_resolver=_resolve_active_agent_for_kill,
        )

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

    agent_name = receiving_agent
    agent_map = get_agent_map()
    if agent_name not in agent_map:
        logger.error("Agent %s not found in agent map", agent_name)
        return

    # Record authoritative active agent for this thread BEFORE any short-
    # circuit. Pack commands (grant / revoke / list packs / who has) need
    # this too: the bot's "paste your token" follow-up is a thread reply
    # without a mention, so handle_message routes it only when this agent
    # is recorded as the thread's active agent.
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
        _spawn_background_task(
            curate_agent_memory(agent_name, agent_config["container"]),
            name=f"curate-memory-{agent_name}",
        )

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
            try:
                await post_approval_message(
                    draft_request=draft_req,
                    agent_name=agent_name,
                    channel=channel,
                    thread_ts=thread_ts,
                    client=client,
                    store=_draft_store,
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


def _agent_owns_dispatch_thread(channel: str, thread_ts: str, agent_name: str) -> bool:
    """True when ``agent_name`` has an in-flight dispatch for (channel, thread_ts).

    Used by :func:`handle_message` to route un-mentioned follow-ups in
    dispatch threads to the correct agent even when no ``active_agent``
    row has been written for the thread (e.g. the dispatch was initiated
    before thread-state was established for this channel+ts pair).

    Best-effort: any exception is logged and treated as False so it never
    blocks routing in the normal path.
    """
    try:
        dispatch_id = _dstate.find_dispatch_for_thread(channel, thread_ts)
        if dispatch_id is None:
            return False
        return _dstate.read_field(dispatch_id, _dstate.FIELD_AGENT) == agent_name
    except Exception:
        logger.exception("Failed to check dispatch thread ownership for channel=%s thread=%s", channel, thread_ts)
        return False


async def handle_message(event, say, client, receiving_agent: str) -> None:
    """Handle DMs and thread follow-ups for ``receiving_agent``.

    Dedup rules with multiple per-agent apps installed in a workspace:

    * DMs are scoped to a single bot, so always handle.
    * Channel messages mentioning *any* known bot are deferred to the
      mentioned agent's ``app_mention`` handler — skip here.
    * Channel thread replies with no mention are handled when this agent
      is the thread's active agent, OR when this agent owns an in-flight
      dispatch for the thread (dispatch threads behave identically to
      direct-mention threads for inbound routing — issue #173).
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
    # OR this agent owns an in-flight dispatch for the thread.
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
        # Dispatch threads route to their owning agent even when active_agent
        # is absent or points to a different agent — the dispatch worker is
        # never interrupted; the reply goes to the agent's normal session.
        if not _agent_owns_dispatch_thread(channel, thread_ts, receiving_agent):
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

    # Start the /healthz HTTP server early so the pull-based deploy
    # daemon can probe us during the initial settle window. The endpoint
    # only flips to 200 once readiness is marked further down — see
    # router/healthz.py for the readiness contract.
    #
    # Port is hardcoded to 8080 inside the container. Compose handles
    # host-side port selection via the HEALTHZ_PORT env var on the host
    # — if the router rebinds to the override, the host→container
    # forward breaks when the two diverge.
    global _healthz_runner
    _healthz_runner = await start_healthz_server(port=8080)

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

    _spawn_background_task(_session_cleanup_loop(), name="session-cleanup-loop")
    _spawn_background_task(_expiration_worker_loop(), name="expiration-worker-loop")

    # Register scheduled-task handlers on each agent's Bolt app. Each agent
    # has a unique slash command (``/<prefix><name>-tasks``), but in a dev
    # deployment a single Slack App often hosts every agent's command, and
    # Socket Mode load-balances across whichever sockets that App has open
    # — so a command sent to "Lisa" can land on Sam's bolt_app and vice
    # versa. To stay correct under any routing, we register *every* agent's
    # command on *every* bolt_app, and resolve the target agent from the
    # command body itself. ``SLASH_COMMAND_PREFIX`` lets a dev deployment
    # (e.g. ``dev-`` prefix) coexist with prod in the same workspace.
    slash_prefix = os.environ.get("SLASH_COMMAND_PREFIX", "")
    all_agent_names = list(get_agent_map().keys())
    all_command_names = [f"/{slash_prefix}{name}-tasks" for name in all_agent_names]
    suffix = "-tasks"

    def _agent_from_command(body: dict) -> str | None:
        cmd = (body.get("command") or "").lstrip("/")
        if slash_prefix:
            cmd = cmd.removeprefix(slash_prefix)
        if not cmd.endswith(suffix):
            return None
        agent = cmd[: -len(suffix)]
        return agent or None

    # One shared store + scheduler for the whole process. Per-bolt_app
    # schedulers all see the same DB, so spinning up N of them caused every
    # due task to be posted under every bot at once.
    scheduled_tasks_store = open_store()

    for agent_name, bolt_app in _apps_by_agent.items():
        setup_scheduled_tasks_handlers(
            bolt_app=bolt_app,
            store=scheduled_tasks_store,
            agent_resolver=_agent_from_command,
            command_name=all_command_names,
        )
        logger.info(
            "Registered scheduled-task commands %s on bolt_app for agent=%s",
            all_command_names,
            agent_name,
        )

    # Resolve a per-task Slack client off ``_apps_by_agent`` so each task
    # posts under its own bot, regardless of which agent's app the scheduler
    # was wired to.
    def _client_for_scheduled_task(agent_name: str) -> Any | None:
        app = _apps_by_agent.get(agent_name)
        return app.client if app is not None else None

    scheduler_task = start_scheduled_tasks_scheduler(
        store=scheduled_tasks_store,
        client_resolver=_client_for_scheduled_task,
        dispatch_fn=dispatch,
    )
    # Park the scheduler task so asyncio's weak-ref bookkeeping can't drop
    # it. Without this the loop can be GC'd mid-flight and scheduled tasks
    # silently stop firing.
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

    # Dispatch discovery — the router-side reconciler that turns a
    # launched-but-unsupervised dispatch dir (handler exited, babysit
    # is running) into a supervision system task. See
    # router/dispatch/discovery.py for why this lives separately from
    # the scheduler. Default-on; the actual supervision still respects
    # ``DISPATCH_SUPERVISION=inline`` from the pack handler side
    # (inline-mode handlers never write a dispatch dir, so discovery
    # finds nothing to register).
    discovery_task = start_discovery_loop(
        scheduled_tasks_store,
        agent_user_id_resolver=_bot_user_id_by_agent.get,
    )
    _background_tasks.add(discovery_task)
    discovery_task.add_done_callback(_background_tasks.discard)

    if not _app_tokens_by_agent:
        logger.error("No agents have Slack credentials; nothing to start")
        return

    # Build Socket Mode handlers now that the event loop is running, then
    # start them all concurrently.
    handlers = [
        AsyncSocketModeHandler(_apps_by_agent[agent_name], app_token)
        for agent_name, app_token in _app_tokens_by_agent.items()
    ]

    # We're now fully wired: auth.test succeeded for each agent, the
    # session-cleanup and scheduled-task loops are running, and we're
    # about to hand off to Socket Mode. From the CD daemon's point of
    # view, the service has reached its "ready" state.
    mark_ready()

    await asyncio.gather(*(handler.start_async() for handler in handlers))


if __name__ == "__main__":
    asyncio.run(main())
