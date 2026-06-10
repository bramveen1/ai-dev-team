"""Unit tests for the wakeup verb handlers (#312)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from router.scheduled_tasks.store import ScheduledTaskStore
from router.scheduled_tasks.wakeup_verbs import (
    cancel_wakeup,
    schedule_wakeup,
    schedule_wakeup_poll,
)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.fixture
def thread_env(monkeypatch):
    """Inject Slack thread context that the verb handlers read from env."""
    monkeypatch.setenv("DISPATCH_CHANNEL", "C_TEST")
    monkeypatch.setenv("DISPATCH_THREAD_TS", "9999.0001")


@pytest.mark.unit
class TestScheduleWakeup:
    def test_happy_path_inserts_row(self, store, thread_env):
        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="check PR")

        assert "task_id" in result
        assert "fires_at" in result
        assert "error" not in result

        task = store.get(result["task_id"])
        assert task is not None
        assert task.one_shot is True
        assert task.agent_name == "sam"

    def test_thread_context_captured_in_payload(self, store, thread_env):
        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="test")

        task = store.get(result["task_id"])
        assert task.payload["channel_id"] == "C_TEST"
        assert task.payload["thread_ts"] == "9999.0001"
        assert task.payload["reason"] == "test"

    def test_fires_at_is_now_plus_delay(self, store, thread_env):
        before = datetime.now(timezone.utc)
        result = schedule_wakeup(store, "sam", delay_seconds=120, reason="x")
        after = datetime.now(timezone.utc)

        fires_at = datetime.fromisoformat(result["fires_at"])
        assert before + timedelta(seconds=119) <= fires_at <= after + timedelta(seconds=121)

    def test_missing_channel_returns_error(self, store, monkeypatch):
        monkeypatch.delenv("DISPATCH_CHANNEL", raising=False)
        monkeypatch.setenv("DISPATCH_THREAD_TS", "9999.0001")

        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="x")

        assert result["error"] == "MissingThreadContext"
        assert "DISPATCH_CHANNEL" in result["message"]

    def test_missing_thread_ts_returns_error(self, store, monkeypatch):
        monkeypatch.setenv("DISPATCH_CHANNEL", "C_TEST")
        monkeypatch.delenv("DISPATCH_THREAD_TS", raising=False)

        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="x")

        assert result["error"] == "MissingThreadContext"
        assert "DISPATCH_THREAD_TS" in result["message"]

    def test_missing_both_env_vars_returns_error(self, store, monkeypatch):
        monkeypatch.delenv("DISPATCH_CHANNEL", raising=False)
        monkeypatch.delenv("DISPATCH_THREAD_TS", raising=False)

        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="x")

        assert result["error"] == "MissingThreadContext"

    def test_destination_is_channel_id(self, store, thread_env):
        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="x")

        task = store.get(result["task_id"])
        assert task.destination == "C_TEST"


@pytest.mark.unit
class TestScheduleWakeupPoll:
    def test_happy_path_inserts_recurring_row(self, store, thread_env):
        result = schedule_wakeup_poll(store, "sam", period_seconds=30, max_attempts=3, reason="poll")

        assert "task_id" in result
        assert "first_fires_at" in result
        assert "error" not in result

        task = store.get(result["task_id"])
        assert task is not None
        assert task.one_shot is False
        assert task.period_seconds == 30
        assert task.payload["attempts_remaining"] == 3
        assert task.payload["max_attempts"] == 3

    def test_thread_context_captured_in_payload(self, store, thread_env):
        result = schedule_wakeup_poll(store, "sam", period_seconds=30, max_attempts=2, reason="poll")

        task = store.get(result["task_id"])
        assert task.payload["channel_id"] == "C_TEST"
        assert task.payload["thread_ts"] == "9999.0001"

    def test_missing_thread_context_returns_error(self, store, monkeypatch):
        monkeypatch.delenv("DISPATCH_CHANNEL", raising=False)
        monkeypatch.delenv("DISPATCH_THREAD_TS", raising=False)

        result = schedule_wakeup_poll(store, "sam", period_seconds=30, max_attempts=2, reason="x")

        assert result["error"] == "MissingThreadContext"

    def test_invalid_period_raises(self, store, thread_env):
        with pytest.raises(ValueError, match="period_seconds"):
            schedule_wakeup_poll(store, "sam", period_seconds=0, max_attempts=3, reason="x")

    def test_invalid_max_attempts_raises(self, store, thread_env):
        with pytest.raises(ValueError, match="max_attempts"):
            schedule_wakeup_poll(store, "sam", period_seconds=30, max_attempts=0, reason="x")


@pytest.mark.unit
class TestCancelWakeup:
    def test_cancel_own_task_succeeds(self, store, thread_env):
        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="x")
        task_id = result["task_id"]

        cancel_result = cancel_wakeup(store, "sam", task_id=task_id)

        assert cancel_result["status"] == "cancelled"
        assert cancel_result["task_id"] == task_id
        assert store.get(task_id) is None

    def test_cancel_other_agents_task_returns_scope_error(self, store, thread_env):
        result = schedule_wakeup(store, "sam", delay_seconds=60, reason="x")
        task_id = result["task_id"]

        cancel_result = cancel_wakeup(store, "lisa", task_id=task_id)

        assert cancel_result["error"] == "ScopeError"
        # sam's task must still exist
        assert store.get(task_id) is not None

    def test_cancel_nonexistent_task_returns_not_found(self, store, thread_env):
        cancel_result = cancel_wakeup(store, "sam", task_id="no-such-task")

        assert cancel_result["error"] == "not_found"
