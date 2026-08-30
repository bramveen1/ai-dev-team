"""Router dispatcher — routes messages to agent containers via Claude Code CLI.

Replaces the echo placeholder with real Docker exec invocations to agent
containers running Claude Code CLI. Uses the spike findings from
docs/spike-claude-cli.md for the CLI invocation pattern.
"""

from __future__ import annotations

import logging
import time

from router import background, runtime, settings, slack_post

# Moved to router.agent_cli (roadmap §2b); re-exported here so existing
# imports and test patch targets keep working.
from router.agent_cli import (  # noqa: F401 — back-compat re-exports
    _API_ERROR_RE,
    CONTAINER_AGENT_MEMORY_FILE,
    CONTAINER_ORG_MEMORY_FILE,
    CONTAINER_PERSONALITY_FILE_TEMPLATE,
    CONTAINER_ROLE_FILE_TEMPLATE,
    CONTAINER_WORLDVIEW_FILE,
    ApiError,
    build_cli_command,
    parse_cli_result,
)
from router.agent_cli import (
    extract_last_tool_use as _extract_last_tool_use,
)
from router.config import get_agent_map

# Moved to router.container_exec (roadmap §2b); re-exported here so existing
# `from router.dispatcher import _run_in_container` call sites and test patch
# targets keep working.
from router.container_exec import (  # noqa: F401 — back-compat re-exports
    DispatchError,
    DispatchTimeoutError,
)
from router.container_exec import (
    run_in_container as _run_in_container,
)
from router.context_builder import build_full_context
from router.memory_loader import load_agent_memory
from router.packs.dispatch_hook import pack_cli_extras
from router.stuck_guard import (
    GuardTrip,
    StuckGuard,
    format_slack_message,
    get_default_guard,
    make_task_id,
    write_post_mortem,
)
from router.thread_loader import load_thread_history, split_messages_at_summary

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_TOKEN_BUDGET = 32000
DEFAULT_MAX_THREAD_MESSAGES = 20
MAX_CONTEXT_TOKENS_ENV = "MAX_CONTEXT_TOKENS"

# Stuck-guard notification ChatAdapter routing (#839, mirrors
# router.dispatch.feed_transport's #713 pattern). Default-off hot flag.
_STATUS_ADAPTER_ENV_FLAG = "DISPATCHER_STATUS_VIA_CHAT_ADAPTER"

# Transports with a live ChatAdapter resolver. Slack is deliberately absent —
# the Slack path always goes through the legacy slack_post call below.
_ADAPTER_TRANSPORTS = frozenset({"discord"})

# Strong references to background tasks so they aren't GC'd before completion
# — shared with router.app via router.background; see that module's docstring.
# Aliased under the old private names for call sites and tests.
_background_tasks = background.background_tasks
_spawn_background_task = background.spawn_background_task


class TaskHaltedError(DispatchError):
    """Raised when the stuck-guard has halted this task and a new dispatch
    is attempted before the task is reset."""

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"Task {task_id} halted by stuck-guard: {reason}")
        self.task_id = task_id
        self.reason = reason


def _resolve_token_budget(explicit_budget: int | None) -> int:
    """Resolve the effective token budget for a dispatch.

    Precedence: explicit arg > ``MAX_CONTEXT_TOKENS`` setting (runtime config
    file, then env var — hot-reloadable via router.settings) > default.
    Invalid stored/env values are warned about inside the settings layer and
    fall back to the registry default (== ``DEFAULT_MAX_TOKEN_BUDGET``).
    """
    if explicit_budget is not None:
        return explicit_budget

    return settings.get(MAX_CONTEXT_TOKENS_ENV)


def _slack_thread_url(client, channel: str, thread_ts: str) -> str | None:
    """Best-effort Slack permalink for the post-mortem footer.

    Returns ``None`` if the client doesn't expose a permalink helper or
    the call fails — we never want post-mortem writing to crash because
    Slack's API blipped.
    """
    fetch = getattr(client, "chat_getPermalink", None)
    if fetch is None:
        return None
    try:
        resp = fetch(channel=channel, message_ts=thread_ts)
    except Exception:
        return None
    if hasattr(resp, "data"):
        resp = resp.data
    if isinstance(resp, dict):
        return resp.get("permalink")
    return None


def _status_adapter_enabled() -> bool:
    """Return True when DISPATCHER_STATUS_VIA_CHAT_ADAPTER is truthy (hot-reloadable)."""
    return bool(settings.get(_STATUS_ADAPTER_ENV_FLAG))


async def _post_via_chat_adapter(*, agent_name: str, conversation_ref: str, text: str) -> None:
    from router.chat.types import ConversationRef, OutboundMessage

    adapter = runtime.discord_adapter_for_agent(agent_name)
    if adapter is None:
        logger.warning("stuck-guard: no discord adapter for agent=%s; skipping post", agent_name)
        return
    try:
        await adapter.send_message(OutboundMessage(text=text, conversation_ref=ConversationRef(conversation_ref)))
    except Exception:
        logger.exception("stuck-guard: ChatAdapter post failed agent=%s", agent_name)


async def _post_stuck_notification(
    *,
    client,
    channel: str,
    thread_ts: str,
    text: str,
    agent_name: str = "",
    conversation_ref: str | None = None,
) -> None:
    """Post the stuck-guard note in the originating conversation. Never raises.

    Behind the default-off ``DISPATCHER_STATUS_VIA_CHAT_ADAPTER`` flag (#839,
    mirrors ``router.dispatch.feed_transport``'s #713 pattern), a dispatch
    carrying a resolvable non-Slack ``conversation_ref`` (structurally
    identified — see ``router.chat.adapters.discord.is_discord_ref``) posts
    through that ChatAdapter instead. Flag off, a Slack/unset transport, or a
    missing ``conversation_ref`` all degrade to the historical
    ``slack_post.best_effort_post`` call, byte-for-byte (preserving
    ``thread_ts``). An unsupported transport skips the post with a clear log
    line; it never silently falls back to Slack (that would post into the
    wrong conversation).

    Errors are swallowed — failing to notify must not mask the underlying
    trip from the dispatcher's caller.
    """
    if _status_adapter_enabled() and conversation_ref:
        from router.chat.adapters.discord import is_discord_ref

        transport = "discord" if is_discord_ref(conversation_ref) else "unknown"
        if transport not in _ADAPTER_TRANSPORTS:
            logger.warning(
                "stuck-guard: unsupported transport=%r for conversation_ref=%r agent=%s; skipping post",
                transport,
                conversation_ref,
                agent_name,
            )
            return
        await _post_via_chat_adapter(agent_name=agent_name, conversation_ref=conversation_ref, text=text)
        return

    await slack_post.best_effort_post(client, channel, text, thread_ts=thread_ts, log=logger, prefix="stuck-guard")


def _handle_guard_trip(
    *,
    guard: StuckGuard,
    trip: GuardTrip,
    task_id: str,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client,
    task_description: str | None,
    conversation_ref: str | None = None,
) -> None:
    """Write the post-mortem and notify Slack in-thread.

    Synchronous on disk; Slack notification is fire-and-forget so the
    network round-trip can't block the next dispatch. Exceptions are
    logged and swallowed — a guard trip already implies we're in a bad
    state, so a notification failure must not cascade.
    """
    state = guard.get_state(task_id)
    if state is None:
        logger.warning("Guard trip with no state for task=%s; skipping post-mortem", task_id)
        return

    try:
        thread_url = _slack_thread_url(client, channel, thread_ts)
    except Exception:
        logger.debug("Could not resolve Slack thread URL for post-mortem", exc_info=True)
        thread_url = None

    try:
        path = write_post_mortem(
            state=state,
            trip=trip,
            config=guard.config,
            slack_thread_url=thread_url,
            task_description=task_description,
        )
    except Exception:
        logger.exception("Failed to write stuck-guard post-mortem")
        path = None

    # In dry-run mode, suppress repeat Slack posts for the same (task_id, trip_kind)
    # so a stuck scheduled lane doesn't spam the channel on every tick.
    # Enforce-mode halts and manual kills always post.
    if not guard.should_notify_trip(task_id, trip.kind):
        logger.debug("Suppressing repeat dry-run Slack notification for task=%s kind=%s", task_id, trip.kind)
        return

    text = format_slack_message(state=state, trip=trip, post_mortem_path=path, config=guard.config)
    _spawn_background_task(
        _post_stuck_notification(
            client=client,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            agent_name=agent_name,
            conversation_ref=conversation_ref,
        ),
        name="stuck-guard-notification",
    )


async def dispatch(
    agent_name: str,
    message: str,
    channel: str,
    thread_ts: str,
    client,
    timeout: int | None = None,
    max_token_budget: int | None = None,
    max_thread_messages: int | None = None,
    bot_user_map: dict[str, str] | None = None,
    guard: StuckGuard | None = None,
    human_initiated: bool = False,
    conversation_ref: str | None = None,
) -> dict:
    """Dispatch a message to an agent container and return the response.

    Loads thread history from Slack, loads agent memory, builds a full
    context (with session resume support), and invokes Claude Code CLI
    inside the agent's Docker container with the agent's role.md as the
    system prompt (replacing Claude Code's default identity). Captures the
    JSON response and returns a result dict.

    Args:
        agent_name: Logical name of the target agent (e.g. "lisa").
        message: The user's message text.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp for threading replies.
        client: Slack WebClient instance (used to fetch thread history).
        timeout: Optional timeout in seconds for the CLI call.
            Defaults to DEFAULT_TIMEOUT_SECONDS (30s).
        max_token_budget: Maximum token budget for conversation context.
            When ``None``, resolves from the ``MAX_CONTEXT_TOKENS`` env var
            and falls back to DEFAULT_MAX_TOKEN_BUDGET (32000).
        max_thread_messages: Maximum thread messages to load.
            Defaults to DEFAULT_MAX_THREAD_MESSAGES (20).
        human_initiated: True when this dispatch was triggered by a genuine
            human message (per the app.py bot-message guard), as opposed to a
            whitelisted dispatch-bot handoff. Resets the stuck-guard turn cap
            and clears a guard-tripped halt for the thread (#422).

    Returns:
        A dict with keys:
            - agent: The agent name that handled the request.
            - status: "ok" on success.
            - response: The agent's response text.

    Raises:
        ValueError: If agent_name is not in the agent map or message is empty.
        DispatchTimeoutError: If the CLI call exceeds the timeout.
        DispatchError: If the CLI exits non-zero, returns empty/invalid output.
    """
    if not message or not message.strip():
        raise ValueError("Message must not be empty")

    agent_map = get_agent_map()
    if agent_name not in agent_map:
        raise ValueError(f"Unknown agent: {agent_name}")

    agent_config = agent_map[agent_name]
    container = agent_config["container"]
    display_name = agent_config.get("name", agent_name.capitalize())
    # Per-agent container_timeout from agent.yaml takes precedence over the
    # globally-configured session_timeout passed via the timeout param.
    # Budget linkage (issue #200):
    #   --max-turns 50  → CLI-side budget (model round-trips)
    #   container_timeout / session_timeout → router-side wall-clock budget
    # At ~20-25 s/turn on Sonnet, 50 turns ≈ 17-21 min → keep wall-clock ≥ 1800s.
    agent_timeout = agent_config.get("container_timeout")
    if agent_timeout is not None:
        effective_timeout = agent_timeout
    elif timeout is not None:
        effective_timeout = timeout
    else:
        effective_timeout = DEFAULT_TIMEOUT_SECONDS
    effective_budget = _resolve_token_budget(max_token_budget)
    effective_max_messages = max_thread_messages if max_thread_messages is not None else DEFAULT_MAX_THREAD_MESSAGES

    # Stuck-guard pre-check. We always honor a halted task — in enforce
    # mode that means "trip detected, stop the bleed", and in dry-run mode
    # only `/kill` can flip `halted` (regular trips just record without
    # halting per spec). So a halted state in either mode means: a human
    # asked us to stop, or the guard caught a runaway in enforce. Either
    # way, refuse to dispatch.
    active_guard = guard if guard is not None else get_default_guard()
    task_id = make_task_id(channel, thread_ts, agent_name)
    # #422: A human re-engaging the thread resets the automated turn cap so a
    # healthy human-steered conversation never bricks at the cap in enforce
    # mode. This also clears a guard-tripped halt (but NOT a manual /kill —
    # that override stays sticky until reset_task), so it must run *before*
    # the is_halted pre-check below to let a human rescue a tripped thread.
    # The sender classification is computed by the caller from the app.py
    # bot-message guard; only genuine human messages set human_initiated.
    if human_initiated:
        active_guard.record_human_message(task_id=task_id, agent_name=agent_name)
    if active_guard.is_halted(task_id):
        state = active_guard.get_state(task_id)
        reason = state.halt_reason.description if state and state.halt_reason else "halted"
        logger.warning("Refusing dispatch — task=%s halted by stuck-guard: %s", task_id, reason)
        raise TaskHaltedError(task_id, reason)

    logger.info(
        "Dispatching to agent=%s container=%s msg_len=%d timeout=%ds",
        agent_name,
        container,
        len(message),
        effective_timeout,
    )

    start_time = time.monotonic()

    # Load thread history from Slack
    thread_history = await load_thread_history(
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        max_messages=effective_max_messages,
    )

    # Check for session summary in thread history (resume from timeout)
    session_summary = None
    context_history = thread_history

    if thread_history:
        session_summary, context_history = split_messages_at_summary(thread_history)
        if session_summary:
            logger.info("Resuming from session summary for agent=%s", agent_name)

    # Load memory context for the agent; the new message keys retrieval of
    # relevant structured memory when MEMORY_RETRIEVAL_ENABLED is set (#640).
    memory = load_agent_memory(agent_name, query_text=message)

    # Resolve bot_user_map agent IDs to their display names so the
    # transcript labels each agent's messages correctly after handoffs.
    display_bot_user_map: dict[str, str] | None = None
    if bot_user_map:
        display_bot_user_map = {
            user_id: agent_map.get(name, {}).get("name", name.capitalize())
            for user_id, name in bot_user_map.items()
            if name in agent_map
        }

    # Build full context with memory + thread history + new message
    context = build_full_context(
        memory=memory,
        thread_history=context_history,
        new_message=message,
        agent_name=display_name,
        session_summary=session_summary,
        max_tokens=effective_budget,
        bot_user_map=display_bot_user_map,
    )

    logger.info("Built context with %d thread messages for agent=%s", len(thread_history), agent_name)

    # Build the Claude CLI command (shared with the transport-neutral seam —
    # see router.agent_cli). Slack context flows into the pack extras so the
    # dispatch pack can inject DISPATCH_CHANNEL/THREAD_TS/AGENT for
    # agent-initiated dispatches.
    extras = pack_cli_extras(agent_name, channel=channel, thread_ts=thread_ts, conversation_ref=conversation_ref)
    cli_cmd = build_cli_command(agent_name, agent_config, extras)

    logger.info("CLI command for agent=%s: %s", agent_name, " ".join(cli_cmd))

    error_class: str | None = None
    try:
        stdout, stderr, returncode = await _run_in_container(
            container,
            cli_cmd,
            effective_timeout,
            stdin_data=context,
            env=extras.env or None,
        )
    except DispatchTimeoutError:
        error_class = "DispatchTimeoutError"
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
            conversation_ref=conversation_ref,
        )
        last_activity_ts = time.strftime("%H:%M:%S UTC", time.gmtime())
        timeout_text = (
            f":alarm_clock: Agent *{display_name}* hit the router timeout ({effective_timeout}s)"
            f" — last activity at {last_activity_ts}."
            f" Likely needs the timeout raised or the task split."
        )
        await _post_stuck_notification(
            client=client,
            channel=channel,
            thread_ts=thread_ts,
            text=timeout_text,
            agent_name=agent_name,
            conversation_ref=conversation_ref,
        )
        raise

    duration = time.monotonic() - start_time

    # Classify the outcome (shared with the transport-neutral seam); every
    # failure path records its error class with the stuck-guard before
    # raising ApiError / DispatchError.
    def _record_error(error_class: str) -> None:
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
            conversation_ref=conversation_ref,
        )

    data, response_text = parse_cli_result(
        agent_name=agent_name,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        record_error=_record_error,
    )

    # Successful turn — record it for guard accounting. Tool name/args come
    # from the CLI's reported last tool use when available; otherwise we
    # log a "text" turn (still counts toward the turn cap).
    last_tool_name, last_tool_args = _extract_last_tool_use(data)
    _record_and_handle_trip(
        guard=active_guard,
        task_id=task_id,
        agent_name=agent_name,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        task_description=message,
        tool_name=last_tool_name,
        tool_args=last_tool_args,
        error_class=None,
        conversation_ref=conversation_ref,
    )

    logger.info(
        "Agent %s responded: response_len=%d duration=%.2fs",
        agent_name,
        len(response_text),
        duration,
    )

    return {
        "agent": agent_name,
        "status": "ok",
        "response": response_text,
    }


def _record_and_handle_trip(
    *,
    guard: StuckGuard,
    task_id: str,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client,
    task_description: str | None,
    tool_name: str | None,
    tool_args: object,
    error_class: str | None,
    conversation_ref: str | None = None,
) -> None:
    """Feed the turn to the guard and run the trip handler if it fired.

    Pulled out so the dispatch error paths and the success path share
    the same handling — every turn the dispatcher observes (good or bad)
    counts toward the guard's accounting.
    """
    trip = guard.record_turn(
        task_id=task_id,
        agent_name=agent_name,
        tool_name=tool_name,
        tool_args=tool_args,
        error_class=error_class,
    )
    if trip is None:
        return
    _handle_guard_trip(
        guard=guard,
        trip=trip,
        task_id=task_id,
        agent_name=agent_name,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        task_description=task_description,
        conversation_ref=conversation_ref,
    )
