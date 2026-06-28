"""Seed data for the scheduled tasks store.

Seeds are discovered from the ``scheduled_tasks`` block of each
``config/agents/*/agent.yaml`` manifest. Adding a recurring task to an agent
is a YAML edit in that agent's directory — no Python changes needed.

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
from pathlib import Path

import yaml

from router.scheduled_tasks import cron
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore

logger = logging.getLogger(__name__)

DEFAULT_AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "agents"


@dataclass(frozen=True)
class SeedTask:
    agent_name: str
    name: str
    prompt: str
    schedule_cron: str
    enabled: bool = False
    destination: str | None = None
    timeout_seconds: int | None = None


def discover_seed_tasks(agents_dir: Path | None = None) -> tuple[SeedTask, ...]:
    """Discover seed tasks from per-agent manifests.

    Reads each ``<agents_dir>/<name>/agent.yaml`` and converts its
    ``scheduled_tasks`` block (if any) into :class:`SeedTask` instances.
    The agent name is taken from the directory, so the idempotency key
    ``(agent_name, name)`` is stable across migrations.
    """
    base = Path(agents_dir) if agents_dir is not None else DEFAULT_AGENTS_DIR
    seeds: list[SeedTask] = []
    if not base.exists():
        return ()

    for agent_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest_path = agent_dir / "agent.yaml"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Skipping seed tasks for %s — failed to parse %s: %s", agent_dir.name, manifest_path, e)
            continue
        if not isinstance(manifest, dict):
            continue

        for entry in manifest.get("scheduled_tasks") or []:
            try:
                schedule_cron = entry["schedule_cron"]
                cron.validate(schedule_cron)
                seeds.append(
                    SeedTask(
                        agent_name=agent_dir.name,
                        name=entry["name"],
                        prompt=entry["prompt"],
                        schedule_cron=schedule_cron,
                        enabled=entry.get("enabled", False),
                        destination=entry.get("destination"),
                        timeout_seconds=entry.get("timeout_seconds"),
                    )
                )
            except (KeyError, TypeError) as e:
                logger.warning(
                    "Skipping malformed scheduled_task in %s — missing field or invalid entry: %s",
                    manifest_path,
                    e,
                )
            except cron.CronError as e:
                logger.warning(
                    "Skipping malformed scheduled_task in %s — invalid schedule_cron: %s",
                    manifest_path,
                    e,
                )

    return tuple(seeds)


DEFAULT_SEED_TASKS: tuple[SeedTask, ...] = discover_seed_tasks()


def seed_default_tasks(
    store: ScheduledTaskStore,
    tasks: tuple[SeedTask, ...] | None = None,
    now: datetime | None = None,
) -> list[ScheduledTask]:
    """Insert each seed task that isn't already present. Returns the rows inserted."""
    if tasks is None:
        tasks = DEFAULT_SEED_TASKS
    now = now or datetime.now(timezone.utc)
    inserted: list[ScheduledTask] = []

    for seed in tasks:
        existing = [t for t in store.list_for_agent(seed.agent_name) if t.name == seed.name]
        if existing:
            logger.debug("Seed task already present: agent=%s name=%s", seed.agent_name, seed.name)
            continue

        try:
            next_run_at = cron.next_run_after(seed.schedule_cron, now)
        except cron.CronError as e:
            logger.warning(
                "Skipping seed task agent=%s name=%s — invalid schedule_cron %r: %s",
                seed.agent_name,
                seed.name,
                seed.schedule_cron,
                e,
            )
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
            next_run_at=next_run_at,
            timeout_seconds=seed.timeout_seconds,
        )
        store.create(task)
        inserted.append(task)
        logger.info("Seeded scheduled task: agent=%s name=%s enabled=%s", seed.agent_name, seed.name, seed.enabled)

    return inserted
