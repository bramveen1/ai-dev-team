"""Seed data for the scheduled tasks store.

Running :func:`seed_default_tasks` is idempotent — it inserts each default
task only if the store has no other task with the same ``(agent_name, name)``
pair. This lets the router boot with sensible defaults without clobbering
tasks a user has already created or modified.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from router.scheduled_tasks import cron
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedTask:
    agent_name: str
    name: str
    prompt: str
    schedule_cron: str
    enabled: bool = False
    destination: str | None = None


DEFAULT_SEED_TASKS: tuple[SeedTask, ...] = (
    SeedTask(
        agent_name="lisa",
        name="Daily inbox review",
        prompt=(
            "Summarize yesterday's inbox activity for Bram: "
            "what came in, what you handled, what still needs his attention. "
            "Post the summary to my DM with Bram."
        ),
        schedule_cron="0 9 * * 1-5",
        enabled=False,
    ),
)


def seed_default_tasks(
    store: ScheduledTaskStore,
    tasks: tuple[SeedTask, ...] = DEFAULT_SEED_TASKS,
    now: datetime | None = None,
) -> list[ScheduledTask]:
    """Insert each seed task that isn't already present. Returns the rows inserted."""
    now = now or datetime.now(timezone.utc)
    inserted: list[ScheduledTask] = []

    for seed in tasks:
        existing = [t for t in store.list_for_agent(seed.agent_name) if t.name == seed.name]
        if existing:
            logger.debug("Seed task already present: agent=%s name=%s", seed.agent_name, seed.name)
            continue

        task = ScheduledTask(
            task_id=str(uuid.uuid4()),
            agent_name=seed.agent_name,
            name=seed.name,
            prompt=seed.prompt,
            schedule_cron=seed.schedule_cron,
            destination=seed.destination,
            enabled=seed.enabled,
            created_at=now,
            next_run_at=cron.next_run_after(seed.schedule_cron, now),
        )
        store.create(task)
        inserted.append(task)
        logger.info("Seeded scheduled task: agent=%s name=%s enabled=%s", seed.agent_name, seed.name, seed.enabled)

    return inserted
