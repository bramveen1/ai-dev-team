"""Slack ``/kill`` slash command — manual stop button for stuck agents.

Per spec, this honors the kill **regardless of guard mode**: even in
``dry-run`` we mark the task halted, write a post-mortem tagged
``manual_kill``, and post a Slack note. v1 has no resume — the human
re-pings the agent with a fresh task to continue.

Usage:

    /kill              → kill the current thread's last-active agent
    /kill <agent>      → kill <agent> in the current thread
    /kill <agent> all  → kill <agent> in every thread we're tracking

The "all" variant is a deliberate escape hatch for a runaway agent
that's looping across multiple threads at once. It is intentionally not
the default to avoid stopping a healthy concurrent thread when the
operator only meant to silence one.
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

ActiveAgentResolver = Callable[[str, str], str | None]


def _parse_kill_args(text: str) -> tuple[str | None, bool]:
    """Return ``(agent_name, all_threads)`` parsed from the slash text.

    Empty text → ``(None, False)`` so the caller can fall back to the
    thread's active agent.
    """
    parts = (text or "").strip().split()
    if not parts:
        return None, False
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
    # ``halt_marker`` into every in-flight dispatch dir the agent owns.
    # The router-side dispatch supervisor (see
    # :mod:`router.dispatch.supervision`) picks up the marker on its
    # next poll, SIGTERMs the subprocess, and posts a ``killed``
    # message in the dispatch's original Slack thread. This is what
    # makes ``/kill sam`` Just Work for active dispatches even though
    # the stuck-guard registry has no concept of subprocess pids
    # (#163).
    halted_dispatches: list[str] = []
    try:
        halted_dispatches = mark_halted_for_agent(target_agent)
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


def _iter_tasks_for_agent(guard: StuckGuard, agent_name: str) -> list[tuple[str, Any]]:
    """Snapshot the live task IDs that belong to ``agent_name``.

    We copy under the guard's lock indirectly via ``get_state``-per-id,
    using the public-ish ``_tasks`` mapping for the listing. The lock
    isn't re-entrant but a brief read here is bounded by the dict size.
    """
    with guard._lock:  # noqa: SLF001 — internal snapshot, kept tight.
        items = [(tid, state) for tid, state in guard._tasks.items() if state.agent_name == agent_name]
    return items


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
            import asyncio

            asyncio.ensure_future(result)
    except Exception:
        logger.exception("Failed to post manual-kill notification")


def register_kill_handler(
    bolt_app: "AsyncApp",
    *,
    command_name: str | list[str] = "/kill",
    guard: StuckGuard | None = None,
    active_agent_resolver: ActiveAgentResolver | None = None,
) -> None:
    """Register the ``/kill`` slash command on a Bolt app.

    ``command_name`` may be a single string or a list — pass a list when
    a dev deployment uses a slash prefix (e.g. ``/dev-kill``) alongside
    the prod command.
    """
    names = [command_name] if isinstance(command_name, str) else list(command_name)
    for cmd in names:

        @bolt_app.command(cmd)
        async def kill_command(ack, body, respond, client):
            await handle_kill_command(
                ack=ack,
                body=body,
                respond=respond,
                client=client,
                guard=guard,
                active_agent_resolver=active_agent_resolver,
            )
