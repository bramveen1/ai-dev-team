"""Tests for the scheduled tasks scheduler loop."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.scheduled_tasks import scheduler
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore


def _make_task(**overrides) -> ScheduledTask:
    defaults = {
        "task_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "name": "Daily inbox review",
        "prompt": "Summarize yesterday's inbox.",
        "schedule_cron": "0 9 * * 1-5",
        "next_run_at": datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc),
        "destination": "C_INBOX",
        "enabled": True,
        "created_at": datetime(2026, 4, 17, 8, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.0"})
    return client


@pytest.fixture
def dispatch_fn():
    return AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "Inbox summary: ..."})


@pytest.mark.unit
@pytest.mark.asyncio
class TestRunOnce:
    async def test_skips_when_no_due_tasks(self, store, slack_client, dispatch_fn):
        store.create(_make_task(next_run_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)))
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        summaries = await scheduler.run_once(store, slack_client, dispatch_fn, now=now)

        assert summaries == []
        dispatch_fn.assert_not_awaited()
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_fires_due_task_and_updates_next_run(self, store, slack_client, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now - timedelta(minutes=1))
        store.create(task)

        summaries = await scheduler.run_once(store, slack_client, dispatch_fn, now=now)

        assert len(summaries) == 1
        assert summaries[0]["task_id"] == task.task_id
        assert summaries[0]["status"] == "ok"

        # dispatch invoked with the agent's prompt
        dispatch_fn.assert_awaited_once()
        call_kwargs = dispatch_fn.call_args.kwargs
        assert call_kwargs["agent_name"] == "lisa"
        assert call_kwargs["message"] == "Summarize yesterday's inbox."

        # response posted to the task's destination
        slack_client.chat_postMessage.assert_awaited_once()
        post_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert post_kwargs["channel"] == "C_INBOX"
        assert "Inbox summary" in post_kwargs["text"]

        # next_run_at rolled forward
        reloaded = store.get(task.task_id)
        assert reloaded.last_run_at == now
        assert reloaded.next_run_at > now

    async def test_skips_disabled_tasks(self, store, slack_client, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(enabled=False, next_run_at=now - timedelta(hours=1))
        store.create(task)

        summaries = await scheduler.run_once(store, slack_client, dispatch_fn, now=now)

        assert summaries == []
        dispatch_fn.assert_not_awaited()

    async def test_dispatch_error_still_advances_next_run(self, store, slack_client):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now)
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("boom"))

        summaries = await scheduler.run_once(store, slack_client, failing_dispatch, now=now)

        assert summaries[0]["status"] == "dispatch_error"
        reloaded = store.get(task.task_id)
        # A failing dispatch must NOT leave the row stuck in the past — otherwise
        # the scheduler would re-fire it on every poll.
        assert reloaded.next_run_at > now
        assert reloaded.last_run_at == now

    async def test_invalid_cron_disables_task(self, store, slack_client, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(schedule_cron="not a cron", next_run_at=now)
        store.create(task)

        await scheduler.run_once(store, slack_client, dispatch_fn, now=now)

        reloaded = store.get(task.task_id)
        assert reloaded.enabled is False

    async def test_missing_destination_logs_but_does_not_raise(self, store, slack_client, dispatch_fn, monkeypatch):
        monkeypatch.delenv("BRAM_DM_CHANNEL", raising=False)
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(destination=None, next_run_at=now)
        store.create(task)

        summaries = await scheduler.run_once(store, slack_client, dispatch_fn, now=now)

        assert summaries[0]["status"] == "no_destination"
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_falls_back_to_bram_dm_env(self, store, slack_client, dispatch_fn, monkeypatch):
        monkeypatch.setenv("BRAM_DM_CHANNEL", "D_BRAM")
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(destination=None, next_run_at=now)
        store.create(task)

        await scheduler.run_once(store, slack_client, dispatch_fn, now=now)

        slack_client.chat_postMessage.assert_awaited_once()
        assert slack_client.chat_postMessage.call_args.kwargs["channel"] == "D_BRAM"


@pytest.mark.unit
@pytest.mark.asyncio
class TestPostFailure:
    async def test_post_failure_marks_summary(self, store, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now)
        store.create(task)

        failing_client = MagicMock()
        failing_client.chat_postMessage = AsyncMock(side_effect=RuntimeError("slack down"))

        summaries = await scheduler.run_once(store, failing_client, dispatch_fn, now=now)

        assert summaries[0]["status"] == "post_failed"
        # Schedule still advanced so the task isn't stuck on the failing post
        assert store.get(task.task_id).next_run_at > now


@pytest.mark.unit
@pytest.mark.asyncio
class TestRunForever:
    async def test_stop_event_terminates_loop(self, store, slack_client, dispatch_fn):
        # No due tasks, scheduler should wake, idle, and exit when stop_event is set.
        stop = asyncio.Event()
        task_coro = asyncio.create_task(
            scheduler.run_forever(store, slack_client, dispatch_fn, poll_interval_seconds=0.05, stop_event=stop)
        )
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task_coro, timeout=1.0)

    async def test_loop_swallows_errors_and_keeps_running(self, store, slack_client, monkeypatch):
        # Force run_once to raise once, then succeed — the loop should survive.
        calls = {"count": 0}

        async def flaky(_store, _client, _dispatch, now=None, timeout=0):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient")
            return []

        monkeypatch.setattr(scheduler, "run_once", flaky)

        stop = asyncio.Event()
        loop_task = asyncio.create_task(
            scheduler.run_forever(store, slack_client, AsyncMock(), poll_interval_seconds=0.01, stop_event=stop)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1.0)

        assert calls["count"] >= 2


@pytest.mark.unit
@pytest.mark.asyncio
class TestAgentIsolation:
    async def test_each_task_invokes_its_own_agent(self, store, slack_client):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        lisa_task = _make_task(agent_name="lisa", next_run_at=now, destination="C_LISA")
        sam_task = _make_task(agent_name="sam", prompt="Sam's task", next_run_at=now, destination="C_SAM")
        store.create(lisa_task)
        store.create(sam_task)

        calls: list[str] = []

        async def dispatch(agent_name, message, channel, thread_ts, client, timeout):
            calls.append(agent_name)
            return {"agent": agent_name, "status": "ok", "response": f"{agent_name} done"}

        await scheduler.run_once(store, slack_client, dispatch, now=now)

        assert sorted(calls) == ["lisa", "sam"]
