"""Tests for scheduled task seed data."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from router.scheduled_tasks.seeds import DEFAULT_SEED_TASKS, SeedTask, seed_default_tasks
from router.scheduled_tasks.store import ScheduledTaskStore

# Fixed fixture tasks used by tests that must run on a bare checkout
# (where config/agents/ does not exist and DEFAULT_SEED_TASKS is empty).
_FIXTURE_TASKS: tuple[SeedTask, ...] = (
    SeedTask(
        agent_name="lisa",
        name="Daily inbox review",
        prompt="Summarize inbox activity.",
        schedule_cron="0 9 * * 1-5",
        enabled=False,
    ),
)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "seeds.db"))
    yield s
    s.close()


@pytest.mark.unit
class TestSeedDefaults:
    def test_lisa_inbox_task_is_default_and_disabled(self):
        lisa_tasks = [t for t in DEFAULT_SEED_TASKS if t.agent_name == "lisa"]
        if not lisa_tasks:
            pytest.skip(
                "No lisa seed tasks in DEFAULT_SEED_TASKS — "
                "populate config/agents/ from config.example/ to run this test"
            )
        inbox = next((t for t in lisa_tasks if "inbox" in t.name.lower()), None)
        assert inbox is not None, "Expected a lisa seed task with 'inbox' in the name"
        assert inbox.enabled is False  # Disabled by default per the issue
        assert inbox.schedule_cron == "0 9 * * 1-5"

    def test_seed_inserts_tasks(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        # Use a fixed fixture task so the test is independent of config/agents/.
        inserted = seed_default_tasks(store, tasks=_FIXTURE_TASKS, now=now)
        assert len(inserted) == len(_FIXTURE_TASKS)

        lisa_tasks = store.list_for_agent("lisa")
        assert len(lisa_tasks) == 1
        assert lisa_tasks[0].enabled is False

    def test_seed_is_idempotent(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        # Use a fixed fixture task so the test is independent of config/agents/.
        first = seed_default_tasks(store, tasks=_FIXTURE_TASKS, now=now)
        second = seed_default_tasks(store, tasks=_FIXTURE_TASKS, now=now)

        assert len(first) == len(_FIXTURE_TASKS)
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
