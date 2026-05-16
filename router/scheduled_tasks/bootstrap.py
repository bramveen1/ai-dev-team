"""Bootstrap helpers for wiring the scheduled tasks subsystem into the router.

Two concerns kept deliberately separate:

* :func:`setup_scheduled_tasks_handlers` — registers the slash-command and
  view-submission handlers on a single Bolt app. Call this **once per
  per-agent Bolt app**, since each app has its own listener registry.
* :func:`start_scheduled_tasks_scheduler` — opens the store, seeds defaults,
  and starts the polling loop that fires due tasks. Call this **exactly
  once** for the process. If you start one scheduler per Bolt app, every
  scheduler sees the same shared store and a single due task ends up
  posted under every agent at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Callable

from router.scheduled_tasks.handlers import register_handlers
from router.scheduled_tasks.scheduler import ClientResolver, run_forever
from router.scheduled_tasks.seeds import seed_default_tasks
from router.scheduled_tasks.store import ScheduledTaskStore

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/scheduled_tasks.db"


def open_store(db_path: str | None = None, seed_defaults: bool = True) -> ScheduledTaskStore:
    """Open the SQLite-backed scheduled tasks store and (optionally) seed it."""
    path = db_path or os.environ.get("SCHEDULED_TASKS_DB", DEFAULT_DB_PATH)
    store = ScheduledTaskStore(path)
    logger.info("Scheduled tasks store opened at %s", path)
    if seed_defaults:
        inserted = seed_default_tasks(store)
        logger.info("Seeded %d default scheduled tasks", len(inserted))
    return store


def setup_scheduled_tasks_handlers(
    bolt_app: AsyncApp,
    store: ScheduledTaskStore,
    agent_resolver: Callable[[dict], str | None],
    command_name: str | list[str] = "/tasks",
) -> None:
    """Register the ``/tasks`` slash-command and create-modal handlers.

    Call once per per-agent Bolt app. ``command_name`` may be a single name
    or a list — pass the list when one app should answer to several
    per-agent commands (every agent's command is registered on every Bolt
    app to tolerate Socket Mode load-balancing between sibling sockets).
    """
    register_handlers(bolt_app, store, agent_resolver=agent_resolver, command_name=command_name)


def start_scheduled_tasks_scheduler(
    store: ScheduledTaskStore,
    client_resolver: ClientResolver,
    dispatch_fn: Callable[..., Any],
) -> asyncio.Task:
    """Start the scheduler polling loop and return its asyncio.Task.

    The caller must keep a strong reference to the returned task — asyncio
    only holds weak refs and a discarded task can be GC'd mid-flight, which
    is exactly how scheduled tasks silently stopped firing in the past.
    """
    return asyncio.create_task(run_forever(store, client_resolver, dispatch_fn))
