"""Scheduler daemon for scheduled tasks.

A lightweight async background loop that periodically scans the scheduled
tasks store for due rows and fires the corresponding agent invocations.

Two task flavors share the same loop:

* **Agent tasks** (no ``callable_ref``) — run the stored prompt through
  :func:`router.dispatcher.dispatch` so the agent gets its full capability
  set (role, personality, memory, tools). The agent's response is posted
  to the task's ``destination`` channel (or, when unset, to a fallback
  channel configured via ``BRAM_DM_CHANNEL``).
* **System tasks** (``callable_ref`` set) — import the dotted path
  (``pkg.module:attr``) and invoke it directly with the stored ``payload``.
  No Claude session is spawned. Used by the dispatch supervision loop
  (:mod:`router.dispatch.supervision`, #163) which polls dispatch state
  files every ~120s and is essentially free per tick. When the callable
  returns ``{"status": "done"}`` the task is deregistered.

After an agent task runs, the scheduler records ``last_run_at`` and
recomputes ``next_run_at`` from the cron expression. System tasks
reschedule by ``now + period_seconds``. A single worker scan is
idempotent and safe to retry.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from router.scheduled_tasks import cron
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore

_ARCHIVED_THREAD_ERRORS = frozenset({"channel_not_found", "is_archived", "thread_not_found"})

logger = logging.getLogger(__name__)

# Strong references to background agent-task coroutines. asyncio only holds
# weak refs to Tasks, so without this set a long-running dispatch can be
# silently GC'd mid-flight (same footgun documented in bootstrap.py).
_background_tasks: set[asyncio.Task] = set()

DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_TASK_TIMEOUT_SECONDS = 300
# Fallback period if a system task is missing ``period_seconds`` for some
# reason — should never happen in practice (the store rejects this at
# create time), but the scheduler guards against it to avoid a divide-by-
# zero-style busy loop where ``next_run_at`` never advances.
DEFAULT_SYSTEM_TASK_PERIOD_SECONDS = 120

# DispatchCallable: (agent_name, prompt, channel, thread_ts, client, timeout) -> result dict
DispatchCallable = Callable[..., Awaitable[dict]]

# Resolves the Slack web client to use for a given agent. The scheduler must
# post output through the *task owner's* bot, not whichever bolt_app it was
# wired with — otherwise every per-agent bolt_app spawns its own scheduler,
# they all see the same shared store, and a single task ends up posted under
# every bot at once.
ClientResolver = Callable[[str], Any]


def resolve_destination(task: ScheduledTask) -> str | None:
    """Resolve the Slack destination for a task's output.

    If the task has an explicit ``destination`` channel, use it. Otherwise fall
    back to the ``BRAM_DM_CHANNEL`` environment variable. Returns None if no
    destination can be resolved (the scheduler logs the output instead).
    """
    if task.destination:
        return task.destination
    return os.environ.get("BRAM_DM_CHANNEL") or None


def _import_callable(ref: str) -> Callable[..., Any]:
    """Import a callable from a dotted ``pkg.module:attr`` reference."""
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise ImportError(f"Invalid callable_ref {ref!r}; expected 'pkg.module:attr'")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


async def _invoke_callable(fn: Callable[..., Any], **kwargs) -> Any:
    """Await ``fn`` if it's a coroutine function, else call it directly."""
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return fn(**kwargs)


async def _run_system_task(
    task: ScheduledTask,
    store: ScheduledTaskStore,
    client_resolver: ClientResolver,
    now: datetime,
    system_client_resolver: ClientResolver | None = None,
) -> dict:
    """Invoke a system task's callable and decide whether to deregister.

    System tasks are scheduled by :meth:`ScheduledTaskStore.create_system_task`
    with a ``callable_ref`` and ``payload``. Each tick we import the
    callable, hand it a Slack client, and inspect the return value:

    * ``{"status": "done", ...}`` — terminal; delete the task.
    * anything else — keep polling; advance ``next_run_at`` by
      ``period_seconds``.

    The client comes from ``system_client_resolver`` when provided, else from
    the regular ``client_resolver``. Issue #270: the dispatch-supervision
    system task reports *on a dispatch*, so production wires
    ``system_client_resolver`` to the workers-bot client — that keeps runtime
    posts on one identity while agent (cron) tasks still post as their agent.
    Callers that don't separate the two (e.g. the supervision smoke test) pass
    only ``client_resolver`` and the system task posts through it unchanged.

    Exceptions raised by the callable are logged and treated as
    "keep polling" so a transient failure (e.g. brief Slack outage) does
    not silently lose the supervision task.
    """
    period = task.period_seconds or DEFAULT_SYSTEM_TASK_PERIOD_SECONDS
    summary: dict[str, Any] = {
        "task_id": task.task_id,
        "status": "ok",
        "kind": "system",
        "callable_ref": task.callable_ref,
    }

    resolver = system_client_resolver or client_resolver
    client = resolver(task.agent_name)
    if client is None:
        logger.warning(
            "No Slack client available for agent=%s on system task %s; will retry next tick",
            task.agent_name,
            task.task_id,
        )
        summary["status"] = "no_client"
        store.update_run_times(task.task_id, last_run_at=now, next_run_at=now + timedelta(seconds=period))
        return summary

    try:
        fn = _import_callable(task.callable_ref or "")
    except (ImportError, AttributeError, ValueError):
        logger.exception(
            "Could not import callable_ref=%s for task %s; disabling task",
            task.callable_ref,
            task.task_id,
        )
        store.set_enabled(task.task_id, enabled=False)
        summary["status"] = "callable_import_error"
        return summary

    payload = task.payload or {}
    try:
        result = await _invoke_callable(fn, payload=payload, slack_client=client, now=now)
    except Exception:
        logger.exception("System task callable raised (task=%s); keeping it scheduled", task.task_id)
        store.update_run_times(task.task_id, last_run_at=now, next_run_at=now + timedelta(seconds=period))
        summary["status"] = "callable_error"
        return summary

    if isinstance(result, dict) and result.get("status") == "done":
        store.delete(task.task_id)
        summary["status"] = "done"
        summary["result"] = result
        return summary

    # Keep polling — advance the run times so list_due picks us up again
    # exactly one period_seconds from now.
    store.update_run_times(task.task_id, last_run_at=now, next_run_at=now + timedelta(seconds=period))
    return summary


def _is_wakeup_task(task: ScheduledTask) -> bool:
    """True when the task carries wakeup thread context in its payload (#312)."""
    return bool(task.payload and "channel_id" in task.payload and "thread_ts" in task.payload)


def _build_wakeup_prompt(task: ScheduledTask) -> str:
    """Construct the wakeup prompt from payload at fire time."""
    payload = task.payload or {}
    reason = payload.get("reason", "")
    thread_ts = payload.get("thread_ts", "")
    attempts_remaining = payload.get("attempts_remaining")
    max_attempts = payload.get("max_attempts")
    lines = [f"[wakeup] reason: {reason}", "", f"thread_ts: {thread_ts}"]
    if attempts_remaining is not None and max_attempts is not None:
        lines.append(f"attempts_remaining: {attempts_remaining}/{max_attempts}")
    return "\n".join(lines)


def _is_archived_thread_error(exc: Exception) -> bool:
    """True when ``exc`` is a Slack API error caused by an archived or gone thread."""
    try:
        from slack_sdk.errors import SlackApiError  # noqa: PLC0415

        if isinstance(exc, SlackApiError):
            return exc.response.get("error") in _ARCHIVED_THREAD_ERRORS
    except ImportError:
        pass
    return False


async def run_task(
    task: ScheduledTask,
    store: ScheduledTaskStore,
    client_resolver: ClientResolver,
    dispatch_fn: DispatchCallable,
    now: datetime | None = None,
    timeout: int = DEFAULT_TASK_TIMEOUT_SECONDS,
    system_client_resolver: ClientResolver | None = None,
) -> dict:
    """Invoke a single scheduled task and persist the new run times.

    Returns a summary dict: for agent tasks ``{"task_id", "status",
    "posted_to", "response_len"}``; for system tasks ``{"task_id",
    "status", "kind", "callable_ref"}`` plus the callable's result on
    terminal. Regardless of the outcome, the next run is advanced so a
    failing task does not busy-loop.

    ``system_client_resolver`` (optional) overrides the client used for system
    tasks only — see :func:`_run_system_task` (#270).
    """
    now = now or datetime.now(timezone.utc)

    # System tasks short-circuit the agent dispatch path entirely.
    if task.is_system_task:
        return await _run_system_task(task, store, client_resolver, now, system_client_resolver)

    is_wakeup = _is_wakeup_task(task)

    if is_wakeup:
        wakeup_payload = task.payload or {}
        channel = wakeup_payload.get("channel_id", "")
        thread_ts = wakeup_payload.get("thread_ts", "")
        message = _build_wakeup_prompt(task)
        destination = channel
    else:
        destination = resolve_destination(task)
        channel = destination or ""
        thread_ts = ""
        message = task.prompt

    summary: dict[str, Any] = {
        "task_id": task.task_id,
        "status": "ok",
        "posted_to": destination,
        "response_len": 0,
    }

    client = client_resolver(task.agent_name)
    if client is None:
        logger.warning(
            "No Slack client available for agent=%s; skipping task %s. Did the agent's app fail to start?",
            task.agent_name,
            task.task_id,
        )
        summary["status"] = "no_client"
    else:
        task_timeout = task.timeout_seconds if task.timeout_seconds is not None else timeout
        try:
            result = await dispatch_fn(
                agent_name=task.agent_name,
                message=message,
                channel=channel,
                thread_ts=thread_ts,
                client=client,
                timeout=task_timeout,
            )
            response_text = result.get("response", "")
            summary["response_len"] = len(response_text)

            if destination:
                try:
                    if response_text.strip() == "__NO_POST__":
                        summary["status"] = "suppressed"
                    else:
                        post_kwargs: dict[str, Any] = {
                            "channel": destination,
                            "text": response_text or f"(no output from {task.agent_name})",
                        }
                        if is_wakeup and thread_ts:
                            post_kwargs["thread_ts"] = thread_ts
                        await client.chat_postMessage(**post_kwargs)
                except Exception as exc:
                    if is_wakeup and _is_archived_thread_error(exc):
                        logger.warning(
                            "wakeup.thread_gone task_id=%s agent=%s reason=%s",
                            task.task_id,
                            task.agent_name,
                            (task.payload or {}).get("reason", ""),
                        )
                        store.delete(task.task_id)
                        summary["status"] = "thread_gone"
                        return summary
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

    # Post-fire scheduling: one-shot → delete; poll wakeup → decrement attempts;
    # regular cron → advance next_run_at from cron expression.
    if task.one_shot:
        store.delete(task.task_id)
    elif is_wakeup:
        # Poll wakeup: decrement attempts_remaining, delete when exhausted.
        payload = task.payload or {}
        remaining = payload.get("attempts_remaining", 1) - 1
        period = task.period_seconds or DEFAULT_SYSTEM_TASK_PERIOD_SECONDS
        if remaining <= 0:
            store.delete(task.task_id)
        else:
            store.update_run_times(task.task_id, last_run_at=now, next_run_at=now + timedelta(seconds=period))
            store.update_payload(task.task_id, {**payload, "attempts_remaining": remaining})
    else:
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
    client_resolver: ClientResolver,
    dispatch_fn: DispatchCallable,
    now: datetime | None = None,
    timeout: int = DEFAULT_TASK_TIMEOUT_SECONDS,
    system_client_resolver: ClientResolver | None = None,
) -> list[dict]:
    """Run one pass: fire every task with ``next_run_at <= now``.

    System tasks execute inline (they're fast Python callables). Agent tasks
    are detached via ``asyncio.create_task`` so a long-running LLM dispatch
    does not block the poll loop from firing other due tasks. Strong
    references are held in ``_background_tasks`` to prevent GC mid-flight.
    Returns only the summaries for inline (system) tasks; agent-task outcomes
    are handled inside their background tasks.
    """
    now = now or datetime.now(timezone.utc)
    due = store.list_due(now)
    if not due:
        return []

    logger.info("Scheduled tasks run_once: %d due tasks", len(due))
    summaries = []
    for task in due:
        if task.is_system_task:
            summary = await run_task(
                task,
                store,
                client_resolver,
                dispatch_fn,
                now=now,
                timeout=timeout,
                system_client_resolver=system_client_resolver,
            )
            summaries.append(summary)
        else:
            # Pre-claim recurring cron tasks: advance next_run_at before
            # detaching so subsequent poll ticks don't re-fire the same row
            # while the first dispatch is still in flight.  last_run_at is
            # left for run_task to record on completion (AC #3).
            if task.schedule_cron:
                try:
                    claimed_next_run = cron.next_run_after(task.schedule_cron, now)
                except cron.CronError:
                    logger.exception(
                        "Invalid cron expression %r on task %s; disabling it",
                        task.schedule_cron,
                        task.task_id,
                    )
                    store.set_enabled(task.task_id, False)
                    continue
                store.advance_next_run_at(task.task_id, claimed_next_run)
            bg = asyncio.create_task(
                run_task(
                    task,
                    store,
                    client_resolver,
                    dispatch_fn,
                    now=now,
                    timeout=timeout,
                    system_client_resolver=system_client_resolver,
                ),
                name=f"scheduled-agent-task-{task.task_id}",
            )
            _background_tasks.add(bg)
            bg.add_done_callback(_background_tasks.discard)
    return summaries


async def drain_agent_tasks() -> list[dict]:
    """Await all pending background agent tasks and return their summaries.

    Intended for use in tests that need to observe the outcome of agent tasks
    dispatched by :func:`run_once`. Production code should not need this.
    """
    if not _background_tasks:
        return []
    results = await asyncio.gather(*list(_background_tasks), return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def run_forever(
    store: ScheduledTaskStore,
    client_resolver: ClientResolver,
    dispatch_fn: DispatchCallable,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: int = DEFAULT_TASK_TIMEOUT_SECONDS,
    stop_event: asyncio.Event | None = None,
    system_client_resolver: ClientResolver | None = None,
) -> None:
    """Scheduler main loop — wakes every ``poll_interval_seconds`` and runs due tasks.

    Pass a ``stop_event`` to support graceful shutdown; otherwise this loops forever.
    ``system_client_resolver`` (optional) routes system-task posts through a
    distinct client — the workers bot for dispatch supervision (#270).
    """
    logger.info("Scheduled tasks scheduler started (interval=%ds)", poll_interval_seconds)
    while True:
        try:
            await run_once(
                store,
                client_resolver,
                dispatch_fn,
                timeout=timeout,
                system_client_resolver=system_client_resolver,
            )
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
