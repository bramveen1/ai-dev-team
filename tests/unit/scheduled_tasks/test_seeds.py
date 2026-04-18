"""Tests for scheduled task seed data."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from router.scheduled_tasks.seeds import DEFAULT_SEED_TASKS, SeedTask, seed_default_tasks
from router.scheduled_tasks.store import ScheduledTaskStore


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "seeds.db"))
    yield s
    s.close()


@pytest.mark.unit
class TestSeedDefaults:
    def test_lisa_inbox_task_is_default_and_disabled(self):
        lisa_tasks = [t for t in DEFAULT_SEED_TASKS if t.agent_name == "lisa"]
        assert lisa_tasks, "Expected at least one Lisa seed task"
        inbox = next(t for t in lisa_tasks if "inbox" in t.name.lower())
        assert inbox.enabled is False  # Disabled by default per the issue
        assert inbox.schedule_cron == "0 9 * * 1-5"

    def test_seed_inserts_tasks(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        inserted = seed_default_tasks(store, now=now)
        assert len(inserted) == len(DEFAULT_SEED_TASKS)

        lisa_tasks = store.list_for_agent("lisa")
        assert len(lisa_tasks) == 1
        assert lisa_tasks[0].enabled is False

    def test_seed_is_idempotent(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        first = seed_default_tasks(store, now=now)
        second = seed_default_tasks(store, now=now)

        assert len(first) == len(DEFAULT_SEED_TASKS)
        assert second == []  # Second run inserts nothing
        assert len(store.list_for_agent("lisa")) == 1

    def test_seed_skips_only_tasks_that_already_exist(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        custom = (
            SeedTask(agent_name="sam", name="Stand-up summary", prompt="Summarize.", schedule_cron="0 9 * * 1-5"),
            SeedTask(agent_name="lisa", name="Daily inbox review", prompt="X", schedule_cron="0 9 * * 1-5"),
        )
        seed_default_tasks(store, tasks=custom[:1], now=now)  # Seed sam's task first
        inserted = seed_default_tasks(store, tasks=custom, now=now)

        # Only the new Lisa task should be inserted the second time
        assert len(inserted) == 1
        assert inserted[0].agent_name == "lisa"
