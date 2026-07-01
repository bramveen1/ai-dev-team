"""Slack ``/kill`` slash command — manual stop button for stuck agents.

Per spec, this honors the kill **regardless of guard mode**: even in
``dry-run`` we mark the task halted, write a post-mortem tagged
``manual_kill``, and post a Slack note. v1 has no resume — the human
re-pings the agent with a fresh task to continue.

Usage:

    /kill              → kill the current thread's last-active agent
    /kill <agent>      → kill <agent> in the current thread
    /kill <agent> all  → kill <agent> in every thread we're tracking
    /kill all          → fleet-wide emergency stop: every agent, every thread

The ``<agent> all`` variant is a deliberate escape hatch for a runaway
agent looping across multiple threads at once.  The bare ``all`` form
(``/kill all``, ``/kill *``, ``/kill everywhere``) is the emergency
fleet-wide stop: it halts every tracked task across every agent and
every thread.  Kill is recoverable — it halts workers, it does not
destroy data — so no confirmation prompt is required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from router.config import get_agent_map
from router.dispatch.supervision import mark_halted_for_agent
from router.stuck_guard import (
    StuckGuard,
    format_slack_message,
    get_default_guard,
    make_task_id,
    write_post_mortem,
)

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from router.commands.types import Command, CommandResult

logger = logging.getLogger(__name__)

# Strong references to in-flight async notification tasks.  Without this,
# asyncio's weak-ref bookkeeping can GC ensure_future() results mid-flight.
_tasks: set[asyncio.Task] = set()

ActiveAgentResolver = Callable[[str, str], str | None]


def _parse_kill_args(text: str) -> tuple[str | None, bool]:
    """Return ``(agent_name, all_threads)`` parsed from the slash text.

    Empty text → ``(None, False)`` so the caller can fall back to the
    thread's active agent.

    A bare broadcast keyword (``all``, ``*``, ``everywhere``) in the
    **first** token position → ``(None, True)``, signalling a fleet-wide
    stop of every agent across every thread.  The caller distinguishes this
    from the empty-input case ``(None, False)`` via the ``all_threads`` flag.
    """
    parts = (text or "").strip().split()
    if not parts:
        return None, False
    if parts[0].lower() in {"all", "*", "everywhere"}:
        return None, True
    agent = parts[0].lower()
    all_threads = len(parts) >= 2 and parts[1].lower() in {"all", "*", "everywhere"}
    return agent, all_threads


async def handle_kill_command(
    *,
    ack: Any,
    body: dict[str, Any],
    respond: Any,
    client: Any,
    guard: StuckGuard | None = None,
    active_agent_resolver: ActiveAgentResolver | None = None,
) -> None:
    """Process one ``/kill`` invocation.

    The handler is split out so unit tests can drive it without spinning
    up a real Bolt app. The Bolt registration in :func:`register_kill_handler`
    is just a thin closure around this.
    """
    await ack()

    active_guard = guard if guard is not None else get_default_guard()
    text = (body.get("text") or "").strip()
    requested_agent, all_threads = _parse_kill_args(text)

    channel = body.get("channel_id") or ""
    thread_ts = body.get("thread_ts") or body.get("message_ts") or ""

    agent_map = get_agent_map()

    # Fleet-wide broadcast: bare /kill all / /kill * / /kill everywhere.
    # Intercept before the agent-name resolution path so we never hit the
    # "Unknown agent `all`" error.
    if requested_agent is None and all_threads:
        result = await _execute_fleet_kill(
            client=client,
            guard=active_guard,
            channel=channel,
            thread_ts=thread_ts,
            agent_map=agent_map,
            requester=body.get("user_id"),
        )
        await respond(text=result.text, response_type="ephemeral")
        return

    # Resolve the target agent. Explicit name in the command wins; without
    # one, fall back to the thread's most recently active agent.
    target_agent = requested_agent
    if target_agent is None and active_agent_resolver is not None and channel and thread_ts:
        try:
            target_agent = active_agent_resolver(channel, thread_ts)
        except Exception:
            logger.exception("active_agent_resolver raised; falling back to error response")

    if not target_agent:
        await respond(
            text=":warning: Specify which agent to kill, e.g. `/kill sam`.",
            response_type="ephemeral",
        )
        return

    if target_agent not in agent_map:
        known = ", ".join(sorted(agent_map.keys())) or "(none configured)"
        await respond(
            text=f":x: Unknown agent `{target_agent}`. Known agents: {known}.",
            response_type="ephemeral",
        )
        return

    killed: list[str] = []
    if all_threads:
        for tid, state in _iter_tasks_for_agent(active_guard, target_agent):
            _kill_one(
                guard=active_guard,
                task_id=tid,
                agent_name=target_agent,
                channel=_channel_from_task_id(tid) or channel,
                thread_ts=_thread_from_task_id(tid) or thread_ts,
                client=client,
            )
            killed.append(tid)
    else:
        if not channel or not thread_ts:
            await respond(
                text=":warning: `/kill` without a thread context needs `<agent> all` to broadcast.",
                response_type="ephemeral",
            )
            return
        task_id = make_task_id(channel, thread_ts, target_agent)
        _kill_one(
            guard=active_guard,
            task_id=task_id,
            agent_name=target_agent,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
        )
        killed.append(task_id)

    # In addition to halting the agent's router-side task, drop a
    # ``halt_marker`` into the in-flight dispatch dir(s) the agent owns.
    # The router-side dispatch supervisor (see
    # :mod:`router.dispatch.supervision`) picks up the marker on its
    # next poll, SIGTERMs the subprocess, and posts a ``killed``
    # message in the dispatch's original Slack thread. This is what
    # makes ``/kill sam`` Just Work for active dispatches even though
    # the stuck-guard registry has no concept of subprocess pids
    # (#163).
    #
    # Scope the marker the same way the guard task is scoped: a bare
    # ``/kill <agent>`` only halts the dispatch in *this* thread, while
    # ``/kill <agent> all`` broadcasts to every thread. Without the
    # thread scope, killing one stuck dispatch SIGTERMs healthy sibling
    # dispatches the agent is running elsewhere — the #256 premature-kill
    # bug.
    halted_dispatches: list[str] = []
    try:
        if all_threads:
            halted_dispatches = mark_halted_for_agent(target_agent)
        else:
            halted_dispatches = mark_halted_for_agent(target_agent, channel=channel, thread_ts=thread_ts)
    except Exception:
        logger.exception("Failed to mark halt_marker for dispatches owned by %s", target_agent)

    if not killed and not halted_dispatches:
        await respond(
            text=f":information_source: No active task to kill for `{target_agent}`.",
            response_type="ephemeral",
        )
        return

    summary_bits = []
    if killed:
        summary_bits.append(f"{len(killed)} task{'s' if len(killed) != 1 else ''}")
    if halted_dispatches:
        summary_bits.append(f"{len(halted_dispatches)} dispatch{'es' if len(halted_dispatches) != 1 else ''}")
    summary = f":octagonal_sign: Killed `{target_agent}` (" + ", ".join(summary_bits) + ")."
    await respond(text=summary, response_type="ephemeral")
    logger.info(
        "Manual kill: agent=%s tasks=%s dispatches=%s requester=%s",
        target_agent,
        killed,
        halted_dispatches,
        body.get("user_id"),
    )


async def _execute_fleet_kill(
    *,
    client: Any,
    guard: StuckGuard,
    channel: str,
    thread_ts: str,
    agent_map: dict,
    requester: str | None,
) -> "CommandResult":
    """Stop every tracked task across every agent (bare ``/kill all`` form).

    Iterates all of ``guard._tasks`` (not filtered by agent), then calls
    ``mark_halted_for_agent`` in broadcast mode for every agent in the map
    to drop halt markers on in-flight dispatches.  Returns a
    :class:`~router.commands.types.CommandResult` with a per-agent breakdown.
    """
    from router.commands.types import CommandResult

    per_agent_tasks: dict[str, list[str]] = {}
    for tid, state in _iter_all_tasks(guard):
        agent_name = state.agent_name
        _kill_one(
            guard=guard,
            task_id=tid,
            agent_name=agent_name,
            channel=_channel_from_task_id(tid) or channel,
            thread_ts=_thread_from_task_id(tid) or thread_ts,
            client=client,
        )
        per_agent_tasks.setdefault(agent_name, []).append(tid)

    per_agent_dispatches: dict[str, list[str]] = {}
    for agent_name in agent_map:
        try:
            halted = mark_halted_for_agent(agent_name)
            if halted:
                per_agent_dispatches[agent_name] = halted
        except Exception:
            logger.exception("Failed to mark halt_marker for dispatches owned by %s", agent_name)

    total_tasks = sum(len(v) for v in per_agent_tasks.values())
    total_dispatches = sum(len(v) for v in per_agent_dispatches.values())

    if not total_tasks and not total_dispatches:
        return CommandResult(text=":information_source: No active tasks to kill fleet-wide.")

    all_agents = sorted(set(list(per_agent_tasks) + list(per_agent_dispatches)))
    breakdown_parts = []
    for agent_name in all_agents:
        t = len(per_agent_tasks.get(agent_name, []))
        d = len(per_agent_dispatches.get(agent_name, []))
        bits = []
        if t:
            bits.append(f"{t} task{'s' if t != 1 else ''}")
        if d:
            bits.append(f"{d} dispatch{'es' if d != 1 else ''}")
        breakdown_parts.append(f"{agent_name}: {', '.join(bits)}")

    summary_bits = []
    if total_tasks:
        summary_bits.append(f"{total_tasks} task{'s' if total_tasks != 1 else ''}")
    if total_dispatches:
        summary_bits.append(f"{total_dispatches} dispatch{'es' if total_dispatches != 1 else ''}")

    summary = ":octagonal_sign: Fleet-wide kill (" + ", ".join(summary_bits) + "): " + "; ".join(breakdown_parts) + "."
    logger.info(
        "Fleet-wide kill: tasks=%s dispatches=%s requester=%s",
        total_tasks,
        total_dispatches,
        requester,
    )
    return CommandResult(text=summary)


def _iter_tasks_for_agent(guard: StuckGuard, agent_name: str) -> list[tuple[str, Any]]:
    """Snapshot the live task IDs that belong to ``agent_name``.

    We copy under the guard's lock indirectly via ``get_state``-per-id,
    using the public-ish ``_tasks`` mapping for the listing. The lock
    isn't re-entrant but a brief read here is bounded by the dict size.
    """
    with guard._lock:  # noqa: SLF001 — internal snapshot, kept tight.
        items = [(tid, state) for tid, state in guard._tasks.items() if state.agent_name == agent_name]
    return items


def _iter_all_tasks(guard: StuckGuard) -> list[tuple[str, Any]]:
    """Snapshot all live task IDs across every agent."""
    with guard._lock:  # noqa: SLF001
        return list(guard._tasks.items())


def _parse_conversation_ref(ref: str | None) -> tuple[str, str]:
    """Extract ``(channel, thread_ts)`` from a ``conversation_ref``.

    Supports Discord (``"discord:guild:channel:thread"``) and Slack
    (``"slack:channel:thread"``).  Returns ``("", "")`` when the ref is
    absent or unrecognised.
    """
    if not ref:
        return "", ""
    if ref.startswith("discord:"):
        parts = ref.removeprefix("discord:").split(":")
        if len(parts) == 3:  # noqa: PLR2004
            return parts[1], parts[2]  # channel_id, thread_id
        return "", ""
    if ref.startswith("slack:"):
        parts = ref.removeprefix("slack:").split(":")
        if len(parts) >= 2:  # noqa: PLR2004
            return parts[0], parts[1]
        return "", ""
    return "", ""


def _channel_from_task_id(task_id: str) -> str | None:
    """Pull channel out of a task_id produced by :func:`make_task_id`."""
    parts = task_id.split(":", 2)
    return parts[0] if len(parts) >= 2 else None


def _thread_from_task_id(task_id: str) -> str | None:
    parts = task_id.split(":", 2)
    return parts[1] if len(parts) >= 3 else None


def _kill_one(
    *,
    guard: StuckGuard,
    task_id: str,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client: Any,
) -> None:
    """Mark a single task killed and emit the side-effects (post-mortem + Slack)."""
    trip = guard.kill(task_id=task_id, agent_name=agent_name, reason="manual_kill")
    state = guard.get_state(task_id)
    if state is None:
        return
    try:
        path = write_post_mortem(
            state=state,
            trip=trip,
            config=guard.config,
        )
    except Exception:
        logger.exception("Failed to write post-mortem for manual kill task=%s", task_id)
        path = None
    text = format_slack_message(state=state, trip=trip, post_mortem_path=path, config=guard.config)
    if channel and thread_ts:
        _post_in_thread(client=client, channel=channel, thread_ts=thread_ts, text=text)


def _post_in_thread(*, client: Any, channel: str, thread_ts: str, text: str) -> None:
    """Best-effort threaded notice — never raises."""
    poster = getattr(client, "chat_postMessage", None)
    if poster is None:
        return
    try:
        result = poster(channel=channel, thread_ts=thread_ts, text=text)
        # When the underlying client is async, schedule the awaitable so
        # we don't block here. Sync clients return synchronously.
        if isinstance(result, Awaitable):
            t = asyncio.ensure_future(result)
            _tasks.add(t)
            t.add_done_callback(_tasks.discard)
    except Exception:
        logger.exception("Failed to post manual-kill notification")


async def execute_kill_command(
    cmd: "Command",
    *,
    guard: StuckGuard | None = None,
    active_agent_resolver: ActiveAgentResolver | None = None,
    client: Any = None,
) -> "CommandResult":
    """Transport-neutral ``kill``/``killall`` verb handler.

    Extracts channel and thread context from ``cmd.conversation_ref`` when
    the transport encodes them there (Discord: ``"discord:guild:channel:thread"``;
    Slack: ``"slack:channel:thread"``).

    Returns a :class:`~router.commands.types.CommandResult` — never calls any
    transport-specific ``respond()`` or ``ack()``.
    """
    from router.commands.types import SCOPE_GLOBAL, CommandResult

    active_guard = guard if guard is not None else get_default_guard()
    agent_map = get_agent_map()

    # Extract channel / thread from conversation_ref when available.
    channel, thread_ts = _parse_conversation_ref(cmd.conversation_ref)

    if cmd.verb == "killall" or cmd.scope == SCOPE_GLOBAL:
        return await _execute_fleet_kill(
            client=client,
            guard=active_guard,
            channel=channel,
            thread_ts=thread_ts,
            agent_map=agent_map,
            requester=cmd.principal_ref,
        )

    # kill: agent-scoped — resolve the addressed agent.
    target_agent: str | None = None

    if active_agent_resolver is not None and channel and thread_ts:
        try:
            target_agent = active_agent_resolver(channel, thread_ts)
        except Exception:
            logger.exception("active_agent_resolver raised; falling back to args")

    if target_agent is None and cmd.args:
        first_arg = cmd.args[0].lower()
        if first_arg not in {"all", "*", "everywhere"}:
            target_agent = first_arg

    if not target_agent:
        return CommandResult(text="error: kill needs an agent — address one", ok=False)

    if target_agent not in agent_map:
        known = ", ".join(sorted(agent_map.keys())) or "(none configured)"
        return CommandResult(
            text=f":x: Unknown agent `{target_agent}`. Known agents: {known}.",
            ok=False,
        )

    all_threads = len(cmd.args) >= 2 and cmd.args[1].lower() in {"all", "*", "everywhere"}

    killed: list[str] = []
    if all_threads:
        for tid, _state in _iter_tasks_for_agent(active_guard, target_agent):
            _kill_one(
                guard=active_guard,
                task_id=tid,
                agent_name=target_agent,
                channel=_channel_from_task_id(tid) or channel,
                thread_ts=_thread_from_task_id(tid) or thread_ts,
                client=client,
            )
            killed.append(tid)
    else:
        if not channel or not thread_ts:
            return CommandResult(text="error: kill needs an agent — address one", ok=False)
        task_id = make_task_id(channel, thread_ts, target_agent)
        _kill_one(
            guard=active_guard,
            task_id=task_id,
            agent_name=target_agent,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
        )
        killed.append(task_id)

    halted_dispatches: list[str] = []
    try:
        if all_threads:
            halted_dispatches = mark_halted_for_agent(target_agent)
        else:
            halted_dispatches = mark_halted_for_agent(target_agent, channel=channel, thread_ts=thread_ts)
    except Exception:
        logger.exception("Failed to mark halt_marker for dispatches owned by %s", target_agent)

    if not killed and not halted_dispatches:
        return CommandResult(text=f":information_source: No active task to kill for `{target_agent}`.")

    summary_bits = []
    if killed:
        summary_bits.append(f"{len(killed)} task{'s' if len(killed) != 1 else ''}")
    if halted_dispatches:
        summary_bits.append(f"{len(halted_dispatches)} dispatch{'es' if len(halted_dispatches) != 1 else ''}")
    summary = f":octagonal_sign: Killed `{target_agent}` (" + ", ".join(summary_bits) + ")."
    logger.info(
        "Manual kill: agent=%s tasks=%s dispatches=%s requester=%s",
        target_agent,
        killed,
        halted_dispatches,
        cmd.principal_ref,
    )
    return CommandResult(text=summary)


async def handle_kill_command_from_parsed(
    cmd: "Command",
    *,
    ack: Any,
    body: dict[str, Any],
    respond: Any,
    client: Any,
    guard: StuckGuard | None = None,
    active_agent_resolver: ActiveAgentResolver | None = None,
) -> None:
    """Slack shim: ack → execute kill → render :class:`~router.commands.types.CommandResult`.

    Called by the Slack forwarding shim in :func:`register_kill_handler`.
    This wrapper is the **only** place in the kill path that references Slack's
    ``respond()`` and ``ack()`` — the underlying :func:`execute_kill_command`
    is transport-neutral.

    ``cmd.verb`` determines the scope (enforced from the static
    :data:`~router.commands.grammar.VERB_TABLE`):

    * ``"kill"``    (SCOPE_AGENT)  — stop the addressed agent's run.
    * ``"killall"`` (SCOPE_GLOBAL) — fleet-wide stop of every agent.
    """
    await ack()

    channel = body.get("channel_id") or ""
    thread_ts = body.get("thread_ts") or body.get("message_ts") or ""

    # Embed channel + thread into the Command so execute_kill_command can
    # extract them from conversation_ref or fall back to these explicit values.
    from router.commands.types import Command as _Cmd

    enriched = _Cmd(
        verb=cmd.verb,
        args=cmd.args,
        scope=cmd.scope,
        subject_ref=cmd.subject_ref,
        conversation_ref=cmd.conversation_ref or (f"slack:{channel}:{thread_ts}" if channel else None),
        principal_ref=cmd.principal_ref or (f"slack:{body.get('user_id')}" if body.get("user_id") else None),
        transport=cmd.transport or "slack",
    )

    result = await execute_kill_command(
        enriched,
        guard=guard,
        active_agent_resolver=active_agent_resolver,
        client=client,
    )
    await respond(text=result.text, response_type="ephemeral")


def register_kill_handler(
    bolt_app: "AsyncApp",
    *,
    command_name: str | list[str] = "/kill",
    guard: StuckGuard | None = None,
    active_agent_resolver: ActiveAgentResolver | None = None,
) -> None:
    """Register the ``/kill`` (and optionally ``/killall``) slash command on a Bolt app.

    The Bolt callback is a forwarding shim only: it strips the slash
    affordance via :func:`~router.commands.slack_shim.parse_slack_slash`,
    then delegates to :func:`handle_kill_command_from_parsed`.

    ``command_name`` may be a single string or a list — pass a list when
    a dev deployment uses a slash prefix (e.g. ``/dev-kill``) alongside
    the prod command.
    """
    from router.commands.slack_shim import parse_slack_slash

    names = [command_name] if isinstance(command_name, str) else list(command_name)
    for cmd in names:

        @bolt_app.command(cmd)
        async def kill_command(ack, body, respond, client, _cmd=cmd):
            channel = body.get("channel_id") or ""
            thread_ts = body.get("thread_ts") or body.get("message_ts") or ""
            conversation_ref = f"slack:{channel}:{thread_ts}" if channel else None
            principal_ref = f"slack:{body.get('user_id')}" if body.get("user_id") else None
            parsed_cmd = parse_slack_slash(
                _cmd,
                body,
                conversation_ref=conversation_ref,
                principal_ref=principal_ref,
            )
            if parsed_cmd is None:
                await ack()
                await respond(text=":x: Unknown command.", response_type="ephemeral")
                return
            await handle_kill_command_from_parsed(
                parsed_cmd,
                ack=ack,
                body=body,
                respond=respond,
                client=client,
                guard=guard,
                active_agent_resolver=active_agent_resolver,
            )
