"""Tests for scheduled task seed data."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone

import pytest

from router.scheduled_tasks.seeds import DEFAULT_SEED_TASKS, SeedTask, discover_seed_tasks, seed_default_tasks
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


@pytest.mark.unit
class TestMalformedSeedHardening:
    """Regression tests for boot-crash on malformed schedule_cron / entry shape."""

    @pytest.fixture
    def store(self, tmp_path):
        s = ScheduledTaskStore(str(tmp_path / "seeds.db"))
        yield s
        s.close()

    def _write_manifest(self, agents_dir, agent_name, yaml_body):
        agent_dir = agents_dir / agent_name
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text(textwrap.dedent(yaml_body))

    def test_discover_skips_too_few_fields_cron(self, tmp_path):
        """4-field cron must be skipped; valid sibling still loads."""
        self._write_manifest(
            tmp_path,
            "alpha",
            """\
            scheduled_tasks:
              - name: bad-task
                prompt: "Do bad thing."
                schedule_cron: "0 9 * *"
              - name: good-task
                prompt: "Do good thing."
                schedule_cron: "0 9 * * 1-5"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        names = [s.name for s in seeds]
        assert "bad-task" not in names
        assert "good-task" in names

    def test_discover_skips_out_of_range_cron(self, tmp_path):
        """Out-of-range cron (minute=99) must be skipped; valid sibling still loads."""
        self._write_manifest(
            tmp_path,
            "beta",
            """\
            scheduled_tasks:
              - name: out-of-range
                prompt: "Bad minute."
                schedule_cron: "0 99 * * *"
              - name: good-task
                prompt: "Good task."
                schedule_cron: "30 8 * * *"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        names = [s.name for s in seeds]
        assert "out-of-range" not in names
        assert "good-task" in names

    def test_discover_skips_non_dict_entry(self, tmp_path):
        """A scalar list item (TypeError) must be skipped; valid sibling still loads."""
        self._write_manifest(
            tmp_path,
            "gamma",
            """\
            scheduled_tasks:
              - "just-a-string"
              - name: good-task
                prompt: "Good task."
                schedule_cron: "0 9 * * 1-5"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        names = [s.name for s in seeds]
        assert "good-task" in names

    def test_discover_skips_bad_cron_across_multiple_agents(self, tmp_path):
        """A bad cron in one agent's manifest must not affect another agent's seeds."""
        self._write_manifest(
            tmp_path,
            "agent-bad",
            """\
            scheduled_tasks:
              - name: bad-cron
                prompt: "Bad."
                schedule_cron: "0 9 * *"
            """,
        )
        self._write_manifest(
            tmp_path,
            "agent-good",
            """\
            scheduled_tasks:
              - name: good-task
                prompt: "Good."
                schedule_cron: "0 9 * * 1-5"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        agents = {s.agent_name for s in seeds}
        assert "agent-bad" not in agents
        assert "agent-good" in agents

    def test_seed_default_tasks_skips_bad_cron_in_seed_pass(self, store):
        """seed_default_tasks must not crash when a SeedTask carries a bad cron."""
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        tasks = (
            SeedTask(agent_name="x", name="bad", prompt="Bad.", schedule_cron="0 9 * *"),
            SeedTask(agent_name="x", name="good", prompt="Good.", schedule_cron="0 9 * * 1-5"),
        )
        inserted = seed_default_tasks(store, tasks=tasks, now=now)
        assert len(inserted) == 1
        assert inserted[0].name == "good"

    def test_seed_default_tasks_warns_on_bad_cron(self, store, caplog):
        """A warning must be logged when a seed is skipped for a bad cron."""
        import logging

        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        tasks = (SeedTask(agent_name="x", name="bad", prompt="Bad.", schedule_cron="0 9 * *"),)
        with caplog.at_level(logging.WARNING, logger="router.scheduled_tasks.seeds"):
            seed_default_tasks(store, tasks=tasks, now=now)
        assert any("bad" in r.message or "invalid" in r.message.lower() for r in caplog.records)

    def test_discover_skips_feb30_cron(self, tmp_path):
        """Feb 30 must be rejected during discovery; valid sibling still loads."""
        self._write_manifest(
            tmp_path,
            "delta",
            """\
            scheduled_tasks:
              - name: feb30
                prompt: "Runs on Feb 30."
                schedule_cron: "0 0 30 2 *"
              - name: valid
                prompt: "Runs daily."
                schedule_cron: "0 9 * * *"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        names = [s.name for s in seeds]
        assert "feb30" not in names
        assert "valid" in names

    def test_discover_skips_apr31_cron(self, tmp_path):
        """Apr 31 must be rejected during discovery."""
        self._write_manifest(
            tmp_path,
            "epsilon",
            """\
            scheduled_tasks:
              - name: apr31
                prompt: "Runs on Apr 31."
                schedule_cron: "0 0 31 4 *"
              - name: valid
                prompt: "Runs daily."
                schedule_cron: "0 9 * * *"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        names = [s.name for s in seeds]
        assert "apr31" not in names
        assert "valid" in names

    def test_discover_accepts_feb29_cron(self, tmp_path):
        """Feb 29 is a valid leap-day schedule and must be discovered."""
        self._write_manifest(
            tmp_path,
            "zeta",
            """\
            scheduled_tasks:
              - name: leap-day
                prompt: "Runs on Feb 29."
                schedule_cron: "0 0 29 2 *"
            """,
        )
        seeds = discover_seed_tasks(tmp_path)
        names = [s.name for s in seeds]
        assert "leap-day" in names

    def test_seed_default_tasks_feb29_seeds_correctly(self, store):
        """A Feb 29 seed task must be inserted with next_run_at on the next leap day."""
        now = datetime(2026, 6, 28, 0, 0, tzinfo=timezone.utc)
        tasks = (SeedTask(agent_name="x", name="leap", prompt="Leap day.", schedule_cron="0 0 29 2 *"),)
        inserted = seed_default_tasks(store, tasks=tasks, now=now)
        assert len(inserted) == 1
        assert inserted[0].next_run_at == datetime(2028, 2, 29, 0, 0, tzinfo=timezone.utc)

    def test_seed_default_tasks_skips_next_run_cron_error(self, store):
        """If next_run_after raises CronError for a seed, seed_default_tasks must
        skip it gracefully rather than propagating the exception (boot-safety)."""
        from unittest.mock import patch

        from router.scheduled_tasks import cron
        from router.scheduled_tasks.cron import next_run_after as _real_next_run

        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        tasks = (
            SeedTask(agent_name="x", name="broken", prompt="Bad.", schedule_cron="0 9 * * 1-5"),
            SeedTask(agent_name="x", name="good", prompt="Good.", schedule_cron="0 9 * * 1-5"),
        )

        call_count = 0

        def _raise_first(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise cron.CronError("forced")
            return _real_next_run(*args, **kwargs)

        with patch("router.scheduled_tasks.seeds.cron.next_run_after", side_effect=_raise_first):
            inserted = seed_default_tasks(store, tasks=tasks, now=now)

        assert len(inserted) == 1
        assert inserted[0].name == "good"
