"""Built-in wakeup verb handlers for the dispatch pack (#312).

Three verbs that let an agent schedule a one-shot or recurring self-wakeup
without a separate pack or agent.yaml grant:

- ``schedule_wakeup(store, agent_name, delay_seconds, reason)``
- ``schedule_wakeup_poll(store, agent_name, period_seconds, max_attempts, reason)``
- ``cancel_wakeup(store, agent_name, task_id)``

Thread context (``DISPATCH_CHANNEL`` / ``DISPATCH_THREAD_TS``) is captured
from the calling agent's invocation environment at schedule time and
re-injected by the scheduler at fire time, so the woken agent's reply lands
in the original Slack thread.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from router.scheduled_tasks.store import (
    SYSTEM_TASK_CRON_MARKER,
    ScheduledTask,
    ScheduledTaskStore,
    ScopeError,
)

# Env vars injected by pack_cli_extras when the agent has the dispatch pack.
_CHANNEL_ENV = "DISPATCH_CHANNEL"
_THREAD_TS_ENV = "DISPATCH_THREAD_TS"


class MissingThreadContext(Exception):
    """Raised when a wakeup verb is called without Slack thread context in env."""


def _capture_thread_context() -> tuple[str, str]:
    """Read channel_id and thread_ts from the invocation environment.

    Raises MissingThreadContext when either is absent — callers must ensure
    the agent was dispatched with a Slack context before scheduling a wakeup.
    """
    channel_id = os.environ.get(_CHANNEL_ENV, "").strip()
    thread_ts = os.environ.get(_THREAD_TS_ENV, "").strip()
    missing = []
    if not channel_id:
        missing.append(_CHANNEL_ENV)
    if not thread_ts:
        missing.append(_THREAD_TS_ENV)
    if missing:
        raise MissingThreadContext(f"Missing env vars required for wakeup thread context: {', '.join(missing)}")
    return channel_id, thread_ts


def schedule_wakeup(
    store: ScheduledTaskStore,
    agent_name: str,
    *,
    delay_seconds: int,
    reason: str,
) -> dict:
    """Insert a one-shot wakeup task that fires once after ``delay_seconds``.

    Returns ``{"task_id": ..., "fires_at": <ISO-8601>}`` on success, or
    ``{"error": "MissingThreadContext", "message": ...}`` when the env lacks
    Slack context.  ``ValueError`` propagates to the caller when the agent has
    hit the pending-task ceiling.
    """
    try:
        channel_id, thread_ts = _capture_thread_context()
    except MissingThreadContext as exc:
        return {"error": "MissingThreadContext", "message": str(exc)}

    payload = {"reason": reason, "channel_id": channel_id, "thread_ts": thread_ts}
    now = datetime.now(timezone.utc)
    fires_at = now + timedelta(seconds=delay_seconds)
    task = store.create_one_shot_task(
        agent_name=agent_name,
        next_run_at=fires_at,
        payload=payload,
        name="wakeup",
        destination=channel_id,
    )
    return {"task_id": task.task_id, "fires_at": fires_at.isoformat()}


def schedule_wakeup_poll(
    store: ScheduledTaskStore,
    agent_name: str,
    *,
    period_seconds: int,
    max_attempts: int,
    reason: str,
) -> dict:
    """Insert a recurring wakeup task that fires up to ``max_attempts`` times.

    After each fire the scheduler decrements ``attempts_remaining`` in the
    payload; when it reaches zero the row is deleted automatically.

    Returns ``{"task_id": ..., "first_fires_at": <ISO-8601>}`` on success.
    """
    if period_seconds <= 0:
        raise ValueError(f"period_seconds must be > 0 (got {period_seconds})")
    if max_attempts <= 0:
        raise ValueError(f"max_attempts must be > 0 (got {max_attempts})")

    try:
        channel_id, thread_ts = _capture_thread_context()
    except MissingThreadContext as exc:
        return {"error": "MissingThreadContext", "message": str(exc)}

    payload = {
        "reason": reason,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "attempts_remaining": max_attempts,
        "max_attempts": max_attempts,
    }
    now = datetime.now(timezone.utc)
    first_fires_at = now + timedelta(seconds=period_seconds)
    task = ScheduledTask(
        task_id=str(uuid.uuid4()),
        agent_name=agent_name,
        name="wakeup_poll",
        prompt="",
        schedule_cron=SYSTEM_TASK_CRON_MARKER,
        destination=channel_id,
        enabled=True,
        created_at=now,
        next_run_at=first_fires_at,
        one_shot=False,
        period_seconds=period_seconds,
        payload=payload,
    )
    store.create(task)
    return {"task_id": task.task_id, "first_fires_at": first_fires_at.isoformat()}


def cancel_wakeup(
    store: ScheduledTaskStore,
    agent_name: str,
    *,
    task_id: str,
) -> dict:
    """Delete a wakeup task owned by ``agent_name``.

    Returns ``{"task_id": ..., "status": "cancelled"}`` on success,
    ``{"error": "ScopeError", ...}`` when the task belongs to another agent,
    or ``{"error": "not_found", ...}`` when the task doesn't exist.
    """
    try:
        task = store.get(task_id, agent_name=agent_name)
    except ScopeError as exc:
        return {"error": "ScopeError", "message": str(exc)}
    if task is None:
        return {"error": "not_found", "task_id": task_id}
    store.delete(task_id)
    return {"task_id": task_id, "status": "cancelled"}
