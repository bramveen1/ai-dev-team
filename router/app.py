"""Router app — multi-agent slack_bolt application.

Constructs one ``AsyncApp`` + ``AsyncSocketModeHandler`` per agent that has
configured Slack credentials. Events are dispatched to the agent whose Bolt
app received them.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import sys
import time
from typing import Any

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from router import log_buffer as _log_buffer
from router.approvals.handlers import register_handlers as register_approval_handlers
from router.approvals.store import Draft, DraftStore
from router.attachments import (
    attachments_enabled,
    build_attachments_block,
    ingest_files,
    log_channel_membership_warnings,
    validate_files,
)
from router.config import get_agent_map, load_config
from router.dispatch import state as _dstate
from router.dispatch.attachments_sweep import register_attachments_sweep
from router.dispatch.discovery import start_discovery_loop
from router.dispatcher import _run_in_container, dispatch
from router.error_classifier import build_error_message, make_correlation_id
from router.healthz import mark_ready, set_wakeup_store
from router.healthz import start_server as start_healthz_server
from router.internal_api import (
    check_token_configured,
    start_internal_server,
)
from router.internal_api import (
    configure as configure_internal_api,
)
from router.kill_command import register_kill_handler
from router.memory_curator import curate_agent_memory, needs_curation
from router.mentions import last_mentioned
from router.merge_queue import register_merge_queue
from router.packs.dispatch_hook import pack_cli_extras
from router.packs.grants import maybe_handle_pack_command, resolve_pending_reply
from router.packs.secret_store import SecretStore
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
from router.thread_loader import SUMMARY_MARKERS
from router.threads.state import get_default_store

load_dotenv()

config = load_config()

logging.basicConfig(
    level=getattr(logging, config["log_level"], logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Install in-memory ring buffer so /logs endpoint can serve recent lines.
_log_buffer.install()
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

# Allowlist of Slack user IDs (U…) for bots that may trigger auto-review.
# Populated at startup from two sources:
#   1. The DISPATCH_BOT_USER_IDS environment variable (comma-separated list)
#      — for external bots / future machine-user identities (#199/#227).
#   2. Each agent's own resolved bot user ID (added after auth.test) — so the
#      supervisor's auto-review handoff, which posts via the receiving agent's
#      own bolt client, isn't dropped by the bot-message guard.
# A loop is theoretically possible if an agent's normal response text contains
# ``<@self>``, but in practice agents do not self-mention outside the
# supervisor handoff. Revisit once dedicated machine-user PATs land (#227);
# at that point the supervisor will post under its own identity and the
# self-id auto-seed can be removed.
_dispatch_bot_user_ids: set[str] = set()

# Resolved user ID of the workers bot (issue #283). Stored separately so the
# @mention carve-out can gate on workers-bot identity without widening the
# general allowlist check.
_workers_bot_user_id: str | None = None

# Strong references to long-lived background tasks. asyncio only keeps weak
# refs to tasks, so a `create_task(...)` whose return value is discarded can
# be garbage collected mid-flight — silently killing the scheduler loop and
# any other "fire and forget" workers. Anything we want to outlive the call
# stack that started it must be parked here.
_background_tasks: set[asyncio.Task] = set()

# Module-level handles for the HTTP servers. Kept alive for the lifetime
# of the process so the aiohttp ``AppRunner`` objects aren't GC'd while
# their sockets are still listening.
_healthz_runner: Any | None = None
_internal_runner: Any | None = None

# ── Event deduplication ──────────────────────────────────────────────────────
# Slack can deliver the same user message via multiple event types (e.g. both
# ``app_mention`` and ``message`` for a human @-mention).  We guard against
# the resulting double-dispatch by remembering the identity of each event we
# have already started processing.
#
# Key: ``client_msg_id`` when present (set for human messages, stable across
# both event types for the same source); fallback to ``(channel, user, ts)``
# for events that lack it (e.g. bot messages that bypass the bot guard).
#
# Store: ``OrderedDict[key, expiry_epoch]`` capped at ``_SEEN_EVENTS_MAX``
# entries.  We evict by FIFO (oldest insertion) when the cap is reached and
# by TTL on each lookup.  In-process global; collisions across agents are
# vanishingly rare and benign.
_SEEN_EVENTS_MAX: int = 1024
_SEEN_EVENTS_TTL: float = 300.0  # seconds
_seen_events: collections.OrderedDict[str | tuple, float] = collections.OrderedDict()


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


def _workers_client() -> AsyncWebClient | None:
    """Return an ``AsyncWebClient`` authenticated as the workers bot, or None.

    The workers app (``WORKERS_BOT_TOKEN``) is the single runtime identity that
    speaks for posts reporting *on a dispatch* — lifecycle acks, slot tracker,
    auto-review handoff (issue #270). Lifecycle acks routed through whichever
    agent's bolt client handled the approval click would render as that agent's
    persona and (for the done-post) ``@``-mention the agent into a self-loop.

    Safe-degrades to ``None`` when the token is unset, so callers fall back to
    the agent client — i.e. exactly today's behaviour in a deployment that has
    not configured the workers app yet. Builds a fresh client per call (cheap:
    construction does no I/O), reading the token at call time so a late-injected
    token is honoured without a restart.
    """
    token = os.environ.get("WORKERS_BOT_TOKEN") or SecretStore().get_str("workers_bot_token")
    if not token:
        return None
    return AsyncWebClient(token=token)


async def _execute_approved_draft(draft: Draft, channel: str, thread_ts: str, client: Any) -> None:
    """Re-dispatch to the owning agent so it actually runs the approved action.

    Wired in as ``execute_callback`` on the approval handlers. The agent
    drafted the action originally — by re-entering its container with the
    approved draft's metadata as the message, it can execute via the same
    pack tools (``gh pr merge``, etc.) it had access to during drafting.

    The agent's response is parsed for further draft blocks (rare) and
    posted back into the same thread, mirroring the regular event path.

    Special case (issue #212): ``dispatch.dispatch_issue`` approvals skip
    the agent CLI re-entry path entirely. Instead, the router shells out
    via ``docker exec`` to ``packs.dispatch.handler.dispatch_issue`` in
    the owning agent's container with ``--approved`` (which sets
    ``_approved=True`` so the gate is bypassed) and
    ``--supervision-mode poll`` (so the handler returns ``launched`` in
    ~3 s instead of blocking until the spawned ``claude -p`` finishes).
    The router itself has no ``~/.claude/`` so calling the handler
    in-process raises ``auth_seed_failed`` (issue #219); running it
    inside the agent container gives the dispatch its normal
    credentials. The terminal ``:white_check_mark: / :x:`` envelope
    arrives later through the router-side discovery + supervision
    loops, not through the docker-exec stdout parse below.
    """
    if draft.capability_instance == "dispatch" and draft.action_verb == "dispatch_issue":
        # Issue #270: every ack in this branch reports *on a dispatch* —
        # launched / done / error envelopes — so it speaks as the workers bot,
        # not the agent persona whose bolt client handled the approval click.
        # Falls back to the agent ``client`` when ``WORKERS_BOT_TOKEN`` is unset.
        lifecycle_client = _workers_client() or client

        # draft.payload mirrors the gate_preview dict produced by
        # packs.dispatch.handler._evaluate_approval_gate — its keys are
        # (issue_url, repo, branch_target, model, est_workspace_path,
        # gate_reason, …), NOT the dispatch_issue() kwargs.
        payload = draft.payload or {}
        issue_url = payload.get("issue_url") or ""
        pr_url_payload = payload.get("pr_url") or ""
        # In existing-PR mode pr_url is set without an issue_url; require at least one.
        if not issue_url and not pr_url_payload:
            logger.error(
                "Approved dispatch_issue draft %s has no issue_url or pr_url in payload — cannot execute",
                draft.draft_id,
            )
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved draft `{draft.draft_id}` missing issue_url/pr_url; nothing executed.",
            )
            return

        agent_name = draft.agent_name
        agent_map = get_agent_map()
        if agent_name not in agent_map:
            logger.error(
                "Approved dispatch_issue draft %s names unknown agent %r — cannot execute",
                draft.draft_id,
                agent_name,
            )
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved draft references unknown agent `{agent_name}`; nothing executed.",
            )
            return

        container = agent_map[agent_name]["container"]
        # Run the handler inside the originating agent's container so it can
        # read ~/.claude/ (the router container has no Claude credentials).
        #
        # ``--supervision-mode poll`` is mandatory here. Without it the
        # handler defaults to ``_supervision_mode()`` which reads
        # ``$DISPATCH_SUPERVISION`` from the agent container's env — that
        # var is unset on every agent in docker-compose, so the handler
        # would fall back to inline mode and block for the full ~30 min
        # budget waiting on the spawned ``claude -p``. The ``docker exec``
        # we're about to make is capped at 120 s, so inline mode would
        # always time out, post a false ``execution failed``, and orphan
        # the (already-spawned) ``claude -p`` grandchild as a PID-1 leak
        # in the agent container — see issue #212 (post-#221) write-up.
        # poll mode returns ``{status: launched, ...}`` in ~3 s; the
        # router-side discovery + supervision loops post the terminal
        # envelope from there.
        cmd = [
            "python",
            "/config/packs/dispatch/handler.py",
            "dispatch_issue",
        ]
        if issue_url:
            cmd += ["--issue-url", issue_url]
        if payload.get("pr_url"):
            cmd += ["--pr-url", str(payload["pr_url"])]
        cmd += [
            "--channel",
            channel,
            "--thread-ts",
            thread_ts,
            "--agent",
            agent_name,
            "--approved",
            "--supervision-mode",
            "poll",
        ]
        if "model" in payload:
            cmd += ["--model", payload["model"]]
        if payload.get("summary"):
            cmd += ["--summary", payload["summary"]]

        logger.info(
            "gate_bypass_via_approval: executing dispatch_issue via docker exec agent=%s container=%s draft=%s",
            agent_name,
            container,
            draft.draft_id,
        )

        # Inject pack-derived env (notably ``WORKERS_BOT_TOKEN``) via the
        # same hook the agent-initiated dispatch path uses
        # (``router/dispatcher.py``). Without this, the handler's #257
        # guard fires ``workers_token_missing`` on every approval-card
        # dispatch — see issue #268. ``pack_cli_extras`` also resolves
        # any pack-declared secret env for the agent, so future packs
        # (github, browser_use, …) pick up symmetric treatment on both
        # docker-exec paths for free.
        extras = pack_cli_extras(agent_name, channel=channel, thread_ts=thread_ts)

        try:
            stdout, stderr, _rc = await _run_in_container(
                container=container,
                command=cmd,
                timeout=120,
                env=extras.env or None,
            )
        except Exception:
            logger.exception("docker exec dispatch_issue failed for approved draft %s", draft.draft_id)
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved, but execution failed for draft `{draft.draft_id}`. Check router logs.",
            )
            return

        try:
            result: dict[str, Any] = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            logger.error(
                "dispatch_issue stdout not valid JSON for draft %s; stderr=%r stdout=%r",
                draft.draft_id,
                stderr[:200],
                stdout[:200],
            )
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved, but handler returned non-JSON for draft `{draft.draft_id}`. Check router logs.",
            )
            return

        status = result.get("status")
        dispatch_id = result.get("dispatch_id", draft.draft_id)
        if status == "launched":
            text = f":rocket: `{dispatch_id}` launched (approved)"
        elif status == "completed":
            text = f":white_check_mark: `{dispatch_id}` done (exit 0)"
        elif status == "failed":
            exitcode = result.get("exitcode", -1)
            if exitcode == -1:
                text = f":warning: `{dispatch_id}` terminated (exit -1)"
            else:
                text = f":x: `{dispatch_id}` failed (exit {exitcode})"
        elif status == "error":
            text = f":x: `{dispatch_id}` error: {result.get('reason', 'unknown')}"
        else:
            text = f":x: `{dispatch_id}` unexpected status: {status}"

        await lifecycle_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
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
        # A card approval is an explicit human action, so it resets the
        # stuck-guard turn cap and clears a guard-tripped halt for the thread
        # (#422) — otherwise an approved action could be silently refused on a
        # thread that had already tripped the cap.
        result = await dispatch(
            agent_name=agent_name,
            message=message,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config.get("session_timeout"),
            bot_user_map=dict(_bot_user_map),
            human_initiated=True,
        )
    except Exception:
        logger.exception("Failed to dispatch execution for approved draft %s", draft.draft_id)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: Approved, but execution failed for draft `{draft.draft_id}`. Check router logs.",
        )
        return

    response_text = result["response"]
    if response_text:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=md_to_slack(response_text),
        )


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


def _system_task_client(agent_name: str) -> Any | None:
    """Resolve the Slack client for a system (dispatch-supervision) task (#270).

    Dispatch-supervision posts report *on a dispatch*, so they speak as the
    workers bot. Prefer the workers client; fall back to the task owner's agent
    client when no ``WORKERS_BOT_TOKEN`` is configured (today's behaviour). The
    scheduler routes only system tasks through this resolver — agent (cron)
    tasks keep posting as their own agent.
    """
    return _workers_client() or _client_for_agent(agent_name)


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


def _has_persona_mention(text: str) -> bool:
    """Return True iff *text* contains a Slack mention token for a known persona bot.

    Scans for ``<@Uxxxxx>`` tokens and checks each extracted user ID against
    ``_bot_user_id_by_agent.values()`` — the set of user IDs that belong to
    configured agent personas (sam-bot, lisa-bot, etc.).  The workers-bot
    user ID is intentionally excluded because ``_bot_user_id_by_agent`` only
    holds persona bots registered via agent Bolt apps.
    """
    persona_uids = set(_bot_user_id_by_agent.values())
    for uid in re.findall(r"<@(U[A-Z0-9_]+)>", text):
        if uid in persona_uids:
            return True
    return False


def _is_dispatch_bot_sender(event: dict, receiving_agent: str) -> bool:
    """Return True iff this bot event originates from a whitelisted dispatch-bot user.

    Whitelisted user IDs come from two sources, merged at startup:
      • The ``DISPATCH_BOT_USER_IDS`` environment variable (external bots).
      • Each agent's own resolved bot user ID, auto-seeded after ``auth.test``.

    The auto-seed is what lets the supervisor's auto-review handoff (posted
    via the receiving agent's own bolt client) get through the bot-message
    guard. ``receiving_agent`` is accepted for symmetry with the call site
    but is not used here — the allowlist is global, since one agent's
    supervisor may legitimately ping another agent's app.

    Workers-bot receives special treatment (issue #283): even though its user
    ID is in the allowlist, it is only allowed through when the
    ``WORKER_MENTION_HANDOFF`` env flag is ``"1"`` *and* the message text
    contains an explicit mention of a known persona bot.  This prevents
    un-mentioned thread routing and echo loops while still enabling the
    worker→agent handoff path.
    """
    sender = event.get("user", "")
    if not sender:
        return False
    if _workers_bot_user_id and sender == _workers_bot_user_id:
        if os.environ.get("WORKER_MENTION_HANDOFF", "0") != "1":
            return False
        text = event.get("text", "") or ""
        return _has_persona_mention(text)
    return sender in _dispatch_bot_user_ids


_ATTACHMENTS_ROOT = "/var/lib/attachments"


def _bump_attachment_thread_mtime(thread_ts: str) -> None:
    """Touch the per-thread attachments dir to refresh its mtime if it exists.

    Called on every real (non-duplicate) event so the attachments GC TTL
    resets for active threads. Does not create the dir — that happens on
    first file ingest (#328/#330).
    """
    if not thread_ts:
        return
    thread_dir = os.path.join(_ATTACHMENTS_ROOT, thread_ts)
    try:
        if os.path.isdir(thread_dir):
            os.utime(thread_dir, None)
    except OSError:
        logger.debug("Failed to bump mtime for attachments thread dir %s", thread_ts)


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

    # Ignore bot messages to avoid loops, but allow whitelisted dispatch-bot senders through.
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        if _is_dispatch_bot_sender(event, receiving_agent):
            # Guard 1 (issue #547): peer/harness summaries are ingested as context
            # only — they must never create a dispatchable turn. The message remains
            # in Slack and will be visible in thread history on B's next real turn.
            if any(marker in text for marker in SUMMARY_MARKERS):
                logger.info(
                    "guard1: skipping dispatch for peer/harness summary agent=%s text=%.80s",
                    receiving_agent,
                    text,
                )
                return
            logger.info(
                "auto_review: whitelisted dispatch-bot sender=%s bypassing guard, agent=%s",
                event.get("user", ""),
                receiving_agent,
            )
        else:
            logger.debug("Ignoring bot message")
            return

    # Deduplicate by message identity.  Slack delivers the same user message
    # via both ``app_mention`` and ``message``.  The first arrival processes
    # it; subsequent arrivals within the TTL window are dropped silently.
    _dedup_key: str | tuple = event.get("client_msg_id") or (
        channel,
        user,
        event.get("ts", ""),
    )
    _now = time.monotonic()
    if _dedup_key in _seen_events:
        if _seen_events[_dedup_key] > _now:
            logger.debug(
                "dedup: dropping duplicate event key=%s agent=%s",
                _dedup_key,
                receiving_agent,
            )
            return
        # Expired entry — remove so we re-process and refresh below.
        del _seen_events[_dedup_key]
    _seen_events[_dedup_key] = _now + _SEEN_EVENTS_TTL
    _seen_events.move_to_end(_dedup_key)
    while len(_seen_events) > _SEEN_EVENTS_MAX:
        _seen_events.popitem(last=False)

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

    # #327: Bump the per-thread attachments dir mtime so active threads are
    # preserved by the GC sweep. No-op when the dir doesn't exist yet.
    if thread_ts:
        _bump_attachment_thread_mtime(thread_ts)

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
    session = find_session_by_thread(
        channel, thread_ts, agent_name=agent_name, timeout_seconds=config.get("session_timeout")
    )
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
        count = 0
        try:
            count = await handle_clean_exit(
                agent_name=agent_name,
                container=agent_config["container"],
                thread_history=session["thread_history"],
                slack_client=client,
                channel=channel,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Error during clean exit for agent %s", agent_name)
        if count > 0:
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

    # #328: File-attachment ingest — download files and prepend [ATTACHMENTS] block.
    dispatch_text = text
    if attachments_enabled() and thread_ts:
        raw_files = event.get("files") or []
        if raw_files:
            agent_creds = config.get("slack_credentials", {}).get(agent_name, {})
            bot_token = agent_creds.get("bot_token", "")
            valid_files, rejection = validate_files(raw_files, thread_ts, attachments_root=_ATTACHMENTS_ROOT)
            if rejection:
                logger.warning(
                    "attachments: rejected agent=%s thread=%s reason=%s",
                    agent_name,
                    thread_ts,
                    rejection,
                )
                try:
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f":no_entry: Could not ingest attachments: {rejection}",
                    )
                except Exception:
                    logger.exception("attachments: failed to post rejection reply")
                return
            if valid_files and bot_token:
                try:
                    paths, conversion_warnings = await ingest_files(
                        valid_files, thread_ts, bot_token, attachments_root=_ATTACHMENTS_ROOT
                    )
                except Exception:
                    logger.exception(
                        "attachments: ingest raised agent=%s thread=%s",
                        agent_name,
                        thread_ts,
                    )
                    # Abort the dispatch — mirrors the validation-rejection path above.
                    # Ingest failures are typically transient (timeout, disk pressure,
                    # conversion crash); aborting forces a clean retry rather than
                    # returning a plausible-looking answer that silently ignored the
                    # user's files.
                    try:
                        await client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=":no_entry: Could not process attachments — please try again.",
                        )
                    except Exception:
                        logger.exception("attachments: failed to post ingest-failure reply")
                    return
                block = build_attachments_block(paths)
                if block:
                    dispatch_text = block + dispatch_text
                for warning in conversion_warnings:
                    try:
                        await client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f":warning: {warning}",
                        )
                    except Exception:
                        logger.exception("attachments: failed to post conversion warning")

    # Dispatch to agent. #422: classify the sender so a genuine human message
    # resets the stuck-guard turn cap. A message that reached here with a
    # bot_id / bot_message subtype is a whitelisted dispatch-bot handoff (see
    # the bot-message guard above), not a human — those must NOT reset the cap.
    human_initiated = not (event.get("bot_id") or event.get("subtype") == "bot_message")
    try:
        result = await dispatch(
            agent_name=agent_name,
            message=dispatch_text,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config.get("session_timeout"),
            bot_user_map=dict(_bot_user_map),
            human_initiated=human_initiated,
        )

        update_activity(session["session_id"])

        response_text = result["response"]
        add_to_thread_history(session["session_id"], {"user": agent_name, "text": response_text})

        if response_text:
            await say(text=md_to_slack(response_text), thread_ts=thread_ts)

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

    except Exception as exc:
        corr_id = make_correlation_id()
        category, user_msg = build_error_message(exc, corr_id)
        logger.error(
            "Dispatch failure corr_id=%s category=%s agent=%s",
            corr_id,
            category,
            agent_name,
            exc_info=True,
        )
        await say(text=user_msg, thread_ts=thread_ts)


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
    * Channel messages mentioning a *different* agent's bot are deferred
      to that bot's ``app_mention`` handler — skip here.
    * Channel messages mentioning the receiving agent's *own* bot are
      handled here as if ``app_mention`` had fired. Slack suppresses
      ``app_mention`` for self-mentions (a bot @-mentioning itself), so
      without this branch the event would be silently dropped — breaking
      the auto-review handoff where a dispatch worker pings its owning
      agent on completion.
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

    # Channel message that @-mentions a *different* known bot → let the
    # mentioned agent's app_mention handler deal with it.
    own_bot_uid = _bot_user_id_by_agent.get(receiving_agent)
    other_bot_mentioned = any(
        f"<@{uid}>" in text for name, uid in _bot_user_id_by_agent.items() if name != receiving_agent
    )
    if other_bot_mentioned:
        logger.warning(
            "routing.dropped reason=not_mentioned channel=%s agent=%s",
            event.get("channel", ""),
            receiving_agent,
        )
        return

    # Self-mention: Slack does not fire app_mention when a bot mentions
    # itself, so handle it here as if it had. This is the path the auto-
    # review post takes when a dispatch worker pings its owning agent.
    #
    # Gate on sender being a known dispatch bot: when a *human* mentions
    # this bot, Slack already fires ``app_mention`` and that path will
    # dispatch — so handling the duplicate ``message`` event here would
    # double-dispatch (issue #262; same family as #239 / #241 / #245).
    sender_is_known_bot = event.get("user", "") in _dispatch_bot_user_ids
    if sender_is_known_bot and own_bot_uid is not None and f"<@{own_bot_uid}>" in text:
        await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=True)
        return

    # Channel thread reply with no mention — handle iff this agent is active
    # OR this agent owns an in-flight dispatch for the thread.
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event.get("channel", "")
    store_error = False
    active_agent: str | None = None
    try:
        active_agent = get_default_store().get_active_agent(channel, thread_ts)
    except Exception:
        logger.exception("Failed to read thread state")
        store_error = True

    if active_agent != receiving_agent:
        # Dispatch threads route to their owning agent even when active_agent
        # is absent or points to a different agent — the dispatch worker is
        # never interrupted; the reply goes to the agent's normal session.
        if not _agent_owns_dispatch_thread(channel, thread_ts, receiving_agent):
            if store_error:
                reason = "store_error"
            elif active_agent is None:
                reason = "no_active_agent"
            else:
                reason = "not_owned"
            logger.warning(
                "routing.dropped reason=%s channel=%s thread_ts=%s agent=%s",
                reason,
                channel,
                thread_ts,
                receiving_agent,
            )
            return

    await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=False)


async def _session_cleanup_loop(interval_seconds: int = 60) -> None:
    """Periodically clean up timed-out sessions and post summaries."""
    agent_map = get_agent_map()
    logger.info("Session cleanup loop started (interval=%ds)", interval_seconds)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            expired = pop_timed_out_sessions(config.get("session_timeout"))
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


async def _resolve_workers_bot_user_id() -> str | None:
    """Resolve the workers Slack app's bot user ID via ``auth.test`` (issue #252).

    The workers app (authenticated by ``WORKERS_BOT_TOKEN``) is post-only by
    design — the router never builds a Bolt app for it, it only needs the bot
    user ID so worker posts arriving on each agent's inbound Events stream pass
    the bot-message guard in ``_handle_event`` instead of being silently
    dropped. Whitelisting is the same trusted-bot exception path PR #234
    created for the agent bots seeing each other.

    Returns the ``U…`` user ID on success, or ``None`` for both safe-degradation
    paths — token unset, or ``auth.test`` failing (invalid token / network).
    Neither is a crash: without the seed, worker posts are dropped by the
    agent-side guard, which is exactly today's behaviour.
    """
    workers_token = os.environ.get("WORKERS_BOT_TOKEN") or SecretStore().get_str("workers_bot_token")
    if not workers_token:
        logger.info("workers_bot_token absent from env and secrets.json — skipping worker bot auto-seed")
        return None
    try:
        client = AsyncWebClient(token=workers_token)
        auth_resp = await client.auth_test()
        return auth_resp["user_id"]
    except SlackApiError as exc:
        error_code = exc.response.get("error", "unknown") if getattr(exc, "response", None) else "unknown"
        logger.warning("Could not resolve workers bot user ID via auth.test: %s", error_code)
        return None
    except Exception as exc:
        logger.warning("Could not resolve workers bot user ID via auth.test: %s", exc)
        return None


async def main():
    """Start the router: run one Socket Mode handler per configured agent."""
    # Fail-fast if the shared internal API token is not set.
    check_token_configured()

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

    # Wire up the internal API (port 8090, compose-network-only).
    # configure() must be called before start_internal_server() so that
    # request handlers can reach the draft store and per-agent Slack clients.
    configure_internal_api(
        store=_draft_store,
        client_resolver=_client_for_agent,
    )
    global _internal_runner
    _internal_runner = await start_internal_server()

    # Load dispatch-bot user ID allowlist from environment.
    global _dispatch_bot_user_ids
    raw_ids = os.environ.get("DISPATCH_BOT_USER_IDS", "")
    _dispatch_bot_user_ids = {uid.strip() for uid in raw_ids.split(",") if uid.strip()}
    if _dispatch_bot_user_ids:
        logger.info("dispatch_bot_user_ids loaded: %s", _dispatch_bot_user_ids)
    else:
        logger.info("DISPATCH_BOT_USER_IDS not set; no bots whitelisted for auto-review")

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

    # Auto-seed the dispatch-bot allowlist with every resolved agent user ID.
    # The supervisor's auto-review handoff posts via the receiving agent's
    # own bolt client (see router.dispatch.supervision._maybe_fire_auto_review),
    # which means the resulting Slack event arrives with ``user`` = the agent's
    # own bot user ID. Without this seed the bot-message guard would silently
    # drop it. External bots (CI, future machine-user identities) are added via
    # DISPATCH_BOT_USER_IDS above; both sources are merged into the same set.
    _auto_seeded = set(_bot_user_id_by_agent.values())

    # Workers app auto-seed (issue #252). The workers app posts to channels all
    # agent apps are members of; those posts land on every agent's inbound
    # Events stream as bot-authored messages. Resolving its bot user ID and
    # merging it into the same global allowlist makes worker posts pass the
    # bot-message guard as bot-authored-but-trusted — same exception the agent
    # bots get above. Safe-degrades to a no-op when the token is unset or the
    # auth.test fails (see _resolve_workers_bot_user_id).
    workers_bot_id = await _resolve_workers_bot_user_id()
    if workers_bot_id:
        _auto_seeded.add(workers_bot_id)
        logger.info("Built worker bot user id auto-seed: workers_bot_id=%s", workers_bot_id)
    global _workers_bot_user_id
    _workers_bot_user_id = workers_bot_id

    _dispatch_bot_user_ids |= _auto_seeded
    logger.info(
        "dispatch_bot_user_ids final: %s (env=%d, auto-seeded=%d)",
        _dispatch_bot_user_ids,
        len(_dispatch_bot_user_ids - _auto_seeded),
        len(_auto_seeded),
    )

    # #328: Channel-membership precheck for file-attachment downloads.
    # When ATTACHMENTS_ENABLED, warn at startup for any agent that is not a
    # member of any channels (DMs are fine; url_private downloads require channel
    # membership). Safe-degrades: API errors are swallowed so startup never blocks.
    if attachments_enabled():
        logger.info("attachments: ATTACHMENTS_ENABLED=true — running channel membership precheck")
        for agent_name, bolt_app in _apps_by_agent.items():
            await log_channel_membership_warnings(bolt_app.client, agent_name)
    else:
        logger.info("attachments: ATTACHMENTS_ENABLED not set — file ingest disabled")

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
    set_wakeup_store(scheduled_tasks_store)

    # #327: Register the singleton attachments GC sweep as a router system
    # task. The sweep must run in the router process — the only container
    # with the attachments mount read-write — so it cannot live in the pack
    # handler (RO there). Idempotent across restarts. agent_name only picks
    # the scheduler's client; the sweep posts nothing.
    if all_agent_names:
        try:
            register_attachments_sweep(scheduled_tasks_store, agent_name=all_agent_names[0])
        except Exception:
            logger.exception("Failed to register attachments GC sweep system task")
    else:
        logger.warning("No agents configured; skipping attachments GC sweep registration")

    # #437: Register the singleton idle auto-merge queue task. Idempotent across
    # restarts. Requires MERGE_QUEUE_REPO to be set; silently skips if absent so
    # deployments that don't use auto-merge are unaffected.
    _merge_queue_repo = os.environ.get("MERGE_QUEUE_REPO", "")
    if _merge_queue_repo and all_agent_names:
        try:
            import router.merge_queue as _mq  # noqa: PLC0415

            register_merge_queue(
                scheduled_tasks_store,
                agent_name=all_agent_names[0],
                repo=_merge_queue_repo,
                pat_path=os.environ.get("MERGE_QUEUE_PAT_PATH", _mq.MERGE_PAT_PATH),
                # Prefer the dedicated merge-queue channel; fall back to the
                # generic scheduled-task destination only if it is unset.
                destination=(os.environ.get("MERGE_QUEUE_CHANNEL") or os.environ.get("BRAM_DM_CHANNEL") or None),
            )
        except Exception:
            logger.exception("Failed to register idle auto-merge queue system task")
    elif not _merge_queue_repo:
        logger.info("MERGE_QUEUE_REPO not set; skipping idle auto-merge registration")

    # #535: Register the singleton autonomous bug-backlog dispatch loop. Gated
    # on AUTO_DISPATCH_REPO so it stays opt-in at the deploy layer; the runtime
    # ``auto_dispatch.enabled`` config flag (default OFF) + ``shadow_mode``
    # (default ON) are the real kill switches — a registered tick simply
    # no-ops (returns ``skipped: disabled``) until an operator flips the flag.
    # Idempotent across restarts (dedup by callable_ref).
    _auto_dispatch_repo = os.environ.get("AUTO_DISPATCH_REPO", "")
    if _auto_dispatch_repo and all_agent_names:
        try:
            import router.auto_dispatch as _ad  # noqa: PLC0415

            _ad.register_auto_dispatch(
                scheduled_tasks_store,
                agent_name=all_agent_names[0],
                repo=_auto_dispatch_repo,
                pat_path=os.environ.get("AUTO_DISPATCH_PAT_PATH", _ad.MERGE_PAT_PATH),
                destination=(os.environ.get("AUTO_DISPATCH_CHANNEL") or os.environ.get("BRAM_DM_CHANNEL") or None),
            )
        except Exception:
            logger.exception("Failed to register autonomous bug-backlog dispatch system task")
    elif not _auto_dispatch_repo:
        logger.info("AUTO_DISPATCH_REPO not set; skipping autonomous bug-backlog dispatch registration")

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
        # Issue #270: dispatch-supervision (system) tasks post as the workers
        # bot so runtime status lines share one identity; agent cron tasks keep
        # using _client_for_scheduled_task above.
        system_client_resolver=_system_task_client,
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
