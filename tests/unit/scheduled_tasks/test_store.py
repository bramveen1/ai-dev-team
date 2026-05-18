"""Tests for the scheduled tasks store — CRUD, scoping, and due-task listing."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from router.scheduled_tasks.store import (
    SYSTEM_TASK_CRON_MARKER,
    ScheduledTask,
    ScheduledTaskStore,
    ScopeError,
)


def _make_task(**overrides) -> ScheduledTask:
    defaults = {
        "task_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "name": "Daily inbox review",
        "prompt": "Summarize yesterday's inbox.",
        "schedule_cron": "0 9 * * 1-5",
        "next_run_at": datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc),
        "destination": None,
        "enabled": True,
        "created_at": datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.mark.unit
class TestCreateAndGet:
    def test_create_and_get(self, store):
        task = _make_task()
        store.create(task)

        result = store.get(task.task_id)
        assert result is not None
        assert result.task_id == task.task_id
        assert result.agent_name == "lisa"
        assert result.name == "Daily inbox review"
        assert result.prompt == "Summarize yesterday's inbox."
        assert result.schedule_cron == "0 9 * * 1-5"
        assert result.enabled is True
        assert result.destination is None
        assert result.next_run_at == datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)

    def test_get_missing_returns_none(self, store):
        assert store.get("nope") is None

    def test_boolean_enabled_roundtrips(self, store):
        store.create(_make_task(enabled=False))
        loaded = store.list_for_agent("lisa")[0]
        assert loaded.enabled is False

    def test_persistence_across_reconnect(self, tmp_path):
        db_path = str(tmp_path / "persist.db")
        s1 = ScheduledTaskStore(db_path)
        task = _make_task(destination="C123")
        s1.create(task)
        s1.close()

        s2 = ScheduledTaskStore(db_path)
        loaded = s2.get(task.task_id)
        assert loaded is not None
        assert loaded.destination == "C123"
        s2.close()


@pytest.mark.unit
class TestListForAgent:
    def test_list_returns_only_agent_owned(self, store):
        lisa_task = _make_task(agent_name="lisa", name="L1")
        sam_task = _make_task(agent_name="sam", name="S1")
        store.create(lisa_task)
        store.create(sam_task)

        lisa_tasks = store.list_for_agent("lisa")
        assert len(lisa_tasks) == 1
        assert lisa_tasks[0].task_id == lisa_task.task_id

        sam_tasks = store.list_for_agent("sam")
        assert len(sam_tasks) == 1
        assert sam_tasks[0].task_id == sam_task.task_id

    def test_list_enabled_only_filters_paused(self, store):
        store.create(_make_task(name="active"))
        store.create(_make_task(name="paused", enabled=False))

        assert len(store.list_for_agent("lisa")) == 2
        assert len(store.list_for_agent("lisa", enabled_only=True)) == 1


@pytest.mark.unit
class TestListDue:
    def test_due_tasks_include_past_and_exact(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        past = _make_task(name="past", next_run_at=now - timedelta(minutes=1))
        exact = _make_task(name="exact", next_run_at=now)
        future = _make_task(name="future", next_run_at=now + timedelta(minutes=5))
        store.create(past)
        store.create(exact)
        store.create(future)

        due = store.list_due(now)
        names = {t.name for t in due}
        assert names == {"past", "exact"}

    def test_due_skips_disabled_tasks(self, store):
        now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        store.create(_make_task(name="disabled", enabled=False, next_run_at=now - timedelta(hours=1)))
        assert store.list_due(now) == []


@pytest.mark.unit
class TestScoping:
    def test_get_with_wrong_agent_raises(self, store):
        task = _make_task(agent_name="lisa")
        store.create(task)
        with pytest.raises(ScopeError):
            store.get(task.task_id, agent_name="sam")

    def test_get_with_correct_agent_returns_task(self, store):
        task = _make_task(agent_name="lisa")
        store.create(task)
        assert store.get(task.task_id, agent_name="lisa").task_id == task.task_id

    def test_set_enabled_respects_scope(self, store):
        task = _make_task(agent_name="lisa")
        store.create(task)
        with pytest.raises(ScopeError):
            store.set_enabled(task.task_id, enabled=False, agent_name="sam")

        # Still enabled after the failed scoped call
        assert store.get(task.task_id).enabled is True

    def test_delete_respects_scope(self, store):
        task = _make_task(agent_name="lisa")
        store.create(task)

        # Sam cannot delete Lisa's task
        assert store.delete(task.task_id, agent_name="sam") is False
        assert store.get(task.task_id) is not None

        # Lisa can delete her own task
        assert store.delete(task.task_id, agent_name="lisa") is True
        assert store.get(task.task_id) is None


@pytest.mark.unit
class TestUpdateRunTimes:
    def test_update_records_last_and_next_run(self, store):
        task = _make_task()
        store.create(task)

        ran_at = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
        next_run = datetime(2026, 4, 21, 9, 0, tzinfo=timezone.utc)
        updated = store.update_run_times(task.task_id, last_run_at=ran_at, next_run_at=next_run)

        assert updated.last_run_at == ran_at
        assert updated.next_run_at == next_run

        reloaded = store.get(task.task_id)
        assert reloaded.last_run_at == ran_at
        assert reloaded.next_run_at == next_run


@pytest.mark.unit
class TestSetEnabled:
    def test_pause_and_resume(self, store):
        task = _make_task()
        store.create(task)

        paused = store.set_enabled(task.task_id, enabled=False, agent_name="lisa")
        assert paused.enabled is False
        assert store.get(task.task_id).enabled is False

        resumed = store.set_enabled(task.task_id, enabled=True, agent_name="lisa")
        assert resumed.enabled is True

    def test_missing_task_raises_keyerror(self, store):
        with pytest.raises(KeyError):
            store.set_enabled("missing", enabled=False)


@pytest.mark.unit
class TestCreateSystemTask:
    def test_basic_creation(self, store):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="sam",
            name="supervise disp-abc",
            callable_ref="router.dispatch.supervision:check_dispatch",
            payload={"dispatch_id": "disp-abc", "channel": "C1", "thread_ts": "1.0", "agent": "sam"},
            period_seconds=120,
            destination="C1",
            now=now,
        )

        assert task.callable_ref == "router.dispatch.supervision:check_dispatch"
        assert task.payload == {"dispatch_id": "disp-abc", "channel": "C1", "thread_ts": "1.0", "agent": "sam"}
        assert task.period_seconds == 120
        assert task.schedule_cron == SYSTEM_TASK_CRON_MARKER
        assert task.is_system_task is True
        assert task.next_run_at == now + timedelta(seconds=120)

    def test_rejects_non_positive_period(self, store):
        with pytest.raises(ValueError):
            store.create_system_task(
                agent_name="sam",
                name="bad",
                callable_ref="pkg.mod:fn",
                payload={},
                period_seconds=0,
            )

    def test_rejects_malformed_callable_ref(self, store):
        with pytest.raises(ValueError):
            store.create_system_task(
                agent_name="sam",
                name="bad",
                callable_ref="not_a_dotted_path",
                payload={},
                period_seconds=60,
            )

    def test_payload_round_trips_through_sqlite(self, store):
        payload = {"dispatch_id": "d1", "nested": {"k": 1}, "list": [1, 2, 3]}
        task = store.create_system_task(
            agent_name="sam",
            name="x",
            callable_ref="pkg.mod:fn",
            payload=payload,
            period_seconds=60,
        )

        reloaded = store.get(task.task_id)
        assert reloaded.payload == payload

    def test_listed_for_agent_alongside_regular_tasks(self, store):
        store.create(_make_task(agent_name="sam", name="regular"))
        store.create_system_task(
            agent_name="sam",
            name="system",
            callable_ref="pkg.mod:fn",
            payload={},
            period_seconds=60,
        )

        tasks = store.list_for_agent("sam")
        names = {t.name for t in tasks}
        assert names == {"regular", "system"}

    def test_list_due_picks_up_system_task_after_period(self, store):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        store.create_system_task(
            agent_name="sam",
            name="x",
            callable_ref="pkg.mod:fn",
            payload={},
            period_seconds=60,
            now=now,
        )

        # Just-created tasks are scheduled `period_seconds` in the future.
        assert store.list_due(now + timedelta(seconds=30)) == []
        due = store.list_due(now + timedelta(seconds=120))
        assert len(due) == 1
        assert due[0].is_system_task

    def test_list_by_callable_ref(self, store):
        store.create_system_task(
            agent_name="sam",
            name="a",
            callable_ref="router.dispatch.supervision:check_dispatch",
            payload={"dispatch_id": "d-a"},
            period_seconds=60,
        )
        store.create_system_task(
            agent_name="sam",
            name="b",
            callable_ref="some.other:fn",
            payload={},
            period_seconds=60,
        )

        listed = store.list_by_callable_ref("router.dispatch.supervision:check_dispatch")
        assert [t.name for t in listed] == ["a"]


@pytest.mark.unit
class TestMigrationOfLegacyDb:
    """Existing deployments have DBs that pre-date the system-task columns."""

    def test_alter_table_adds_missing_columns(self, tmp_path):
        # Hand-craft a legacy DB with the original column set only.
        db_path = str(tmp_path / "legacy.db")
        legacy = sqlite3.connect(db_path)
        legacy.execute(
            """
            CREATE TABLE scheduled_tasks (
              task_id TEXT PRIMARY KEY,
              agent_name TEXT NOT NULL,
              name TEXT NOT NULL,
              prompt TEXT NOT NULL,
              schedule_cron TEXT NOT NULL,
              destination TEXT,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TIMESTAMP NOT NULL,
              last_run_at TIMESTAMP,
              next_run_at TIMESTAMP NOT NULL
            )
            """
        )
        legacy.commit()
        legacy.close()

        # Opening through the store must add the new columns idempotently
        # without losing existing data.
        store = ScheduledTaskStore(db_path)
        try:
            cols = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(scheduled_tasks)")  # noqa: SLF001
            }
            assert {"callable_ref", "payload", "period_seconds"} <= cols
            # Re-opening is idempotent (no duplicate-column error).
            store2 = ScheduledTaskStore(db_path)
            store2.close()
        finally:
            store.close()
