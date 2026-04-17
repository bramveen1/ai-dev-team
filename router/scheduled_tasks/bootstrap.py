"""Bootstrap helpers for wiring the scheduled tasks subsystem into the router.

Keeps ``router/app.py`` thin: it can call :func:`setup_scheduled_tasks` to
get a configured store, register the ``/tasks`` handlers, seed defaults, and
start the scheduler loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Callable

from router.scheduled_tasks.handlers import register_handlers
from router.scheduled_tasks.scheduler import run_forever
from router.scheduled_tasks.seeds import seed_default_tasks
from router.scheduled_tasks.store import ScheduledTaskStore

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "scheduled_tasks.db"


def setup_scheduled_tasks(
    bolt_app: AsyncApp,
    slack_client: Any,
    dispatch_fn: Callable[..., Any],
    agent_resolver: Callable[[dict], str | None],
    db_path: str | None = None,
    seed_defaults: bool = True,
) -> tuple[ScheduledTaskStore, asyncio.Task]:
    """Initialize the scheduled tasks store, slash command handlers, and scheduler loop.

    Returns ``(store, scheduler_task)``. The caller should keep a reference to
    the store (for shutdown) and can await the scheduler task on shutdown.
    """
    path = db_path or os.environ.get("SCHEDULED_TASKS_DB", DEFAULT_DB_PATH)
    store = ScheduledTaskStore(path)
    logger.info("Scheduled tasks store opened at %s", path)

    if seed_defaults:
        inserted = seed_default_tasks(store)
        logger.info("Seeded %d default scheduled tasks", len(inserted))

    register_handlers(bolt_app, store, agent_resolver=agent_resolver)

    scheduler_task = asyncio.create_task(run_forever(store, slack_client, dispatch_fn))
    return store, scheduler_task
