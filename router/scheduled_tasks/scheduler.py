"""Scheduler daemon for scheduled tasks.

A lightweight async background loop that periodically scans the scheduled
tasks store for due rows and fires the corresponding agent invocations.

Each due task is run through :func:`router.dispatcher.dispatch` so the agent
gets its full capability set (role, personality, memory, tools). The agent's
response is posted to the task's ``destination`` channel (or, when unset, to
a fallback channel configured via ``BRAM_DM_CHANNEL``).

After a task runs, the scheduler records ``last_run_at`` and recomputes
``next_run_at`` from the cron expression. A single worker scan is idempotent
and safe to retry.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from router.scheduled_tasks import cron
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_TASK_TIMEOUT_SECONDS = 300

# DispatchCallable: (agent_name, prompt, channel, thread_ts, client, timeout) -> result dict
DispatchCallable = Callable[..., Awaitable[dict]]


def resolve_destination(task: ScheduledTask) -> str | None:
    """Resolve the Slack destination for a task's output.

    If the task has an explicit ``destination`` channel, use it. Otherwise fall
    back to the ``BRAM_DM_CHANNEL`` environment variable. Returns None if no
    destination can be resolved (the scheduler logs the output instead).
    """
    if task.destination:
        return task.destination
    return os.environ.get("BRAM_DM_CHANNEL") or None


async def run_task(
    task: ScheduledTask,
    store: ScheduledTaskStore,
    client: Any,
    dispatch_fn: DispatchCallable,
    now: datetime | None = None,
    timeout: int = DEFAULT_TASK_TIMEOUT_SECONDS,
) -> dict:
    """Invoke a single scheduled task and persist the new run times.

    Returns a summary dict: ``{"task_id", "status", "posted_to", "response_len"}``.
    ``status`` is one of ``"ok"``, ``"dispatch_error"``, or ``"no_destination"``.
    Regardless of the outcome, ``last_run_at`` and ``next_run_at`` are updated
    so a failing task does not busy-loop.
    """
    now = now or datetime.now(timezone.utc)
    destination = resolve_destination(task)
    summary: dict[str, Any] = {
        "task_id": task.task_id,
        "status": "ok",
        "posted_to": destination,
        "response_len": 0,
    }

    try:
        result = await dispatch_fn(
            agent_name=task.agent_name,
            message=task.prompt,
            channel=destination or "",
            thread_ts="",
            client=client,
            timeout=timeout,
        )
        response_text = result.get("response", "")
        summary["response_len"] = len(response_text)

        if destination:
            try:
                await client.chat_postMessage(
                    channel=destination,
                    text=response_text or f"(no output from {task.agent_name})",
                )
            except Exception:
                logger.exception("Failed to post scheduled task output for task=%s", task.task_id)
                summary["status"] = "post_failed"
        else:
            logger.warning(
                "Scheduled task %s has no destination and BRAM_DM_CHANNEL is not set; response was: %s",
                task.task_id,
                response_text[:200],
            )
            summary["status"] = "no_destination"

    except Exception:
        logger.exception("Dispatch failed for scheduled task %s (agent=%s)", task.task_id, task.agent_name)
        summary["status"] = "dispatch_error"

    try:
        next_run = cron.next_run_after(task.schedule_cron, now)
    except cron.CronError:
        logger.exception("Invalid cron expression %r on task %s; disabling it", task.schedule_cron, task.task_id)
        store.set_enabled(task.task_id, False)
        return summary

    store.update_run_times(task.task_id, last_run_at=now, next_run_at=next_run)
    return summary


async def run_once(
    store: ScheduledTaskStore,
    client: Any,
    dispatch_fn: DispatchCallable,
    now: datetime | None = None,
    timeout: int = DEFAULT_TASK_TIMEOUT_SECONDS,
) -> list[dict]:
    """Run one pass: fire every task with ``next_run_at <= now``."""
    now = now or datetime.now(timezone.utc)
    due = store.list_due(now)
    if not due:
        return []

    logger.info("Scheduled tasks run_once: %d due tasks", len(due))
    summaries = []
    for task in due:
        summary = await run_task(task, store, client, dispatch_fn, now=now, timeout=timeout)
        summaries.append(summary)
    return summaries


async def run_forever(
    store: ScheduledTaskStore,
    client: Any,
    dispatch_fn: DispatchCallable,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: int = DEFAULT_TASK_TIMEOUT_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Scheduler main loop — wakes every ``poll_interval_seconds`` and runs due tasks.

    Pass a ``stop_event`` to support graceful shutdown; otherwise this loops forever.
    """
    logger.info("Scheduled tasks scheduler started (interval=%ds)", poll_interval_seconds)
    while True:
        try:
            await run_once(store, client, dispatch_fn, timeout=timeout)
        except Exception:
            logger.exception("Unhandled error in scheduled tasks run_once")

        if stop_event is not None and stop_event.is_set():
            logger.info("Scheduled tasks scheduler stopping (stop_event set)")
            return

        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
                logger.info("Scheduled tasks scheduler stopping (stop_event set)")
                return
            await asyncio.sleep(poll_interval_seconds)
        except asyncio.TimeoutError:
            continue
