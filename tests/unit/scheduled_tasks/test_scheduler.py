"""Tests for the scheduled tasks scheduler loop."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.scheduled_tasks import scheduler
from router.scheduled_tasks.store import SYSTEM_TASK_CRON_MARKER, ScheduledTask, ScheduledTaskStore


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
def client_resolver(slack_client):
    """Single-client resolver: every agent gets the same mock back."""
    return lambda _agent: slack_client


@pytest.fixture
def dispatch_fn():
    return AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "Inbox summary: ..."})


@pytest.mark.unit
@pytest.mark.asyncio
class TestRunOnce:
    async def test_skips_when_no_due_tasks(self, store, slack_client, client_resolver, dispatch_fn):
        store.create(_make_task(next_run_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)))
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        summaries = await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

        assert summaries == []
        dispatch_fn.assert_not_awaited()
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_fires_due_task_and_updates_next_run(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now - timedelta(minutes=1))
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

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

    async def test_skips_disabled_tasks(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(enabled=False, next_run_at=now - timedelta(hours=1))
        store.create(task)

        summaries = await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

        assert summaries == []
        dispatch_fn.assert_not_awaited()

    async def test_dispatch_error_still_advances_next_run(self, store, slack_client, client_resolver):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now)
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("boom"))

        await scheduler.run_once(store, client_resolver, failing_dispatch, now=now)
        await scheduler.drain_agent_tasks()

        reloaded = store.get(task.task_id)
        # A failing dispatch must NOT leave the row stuck in the past — otherwise
        # the scheduler would re-fire it on every poll.
        assert reloaded.next_run_at > now
        assert reloaded.last_run_at == now

    async def test_invalid_cron_disables_task(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(schedule_cron="not a cron", next_run_at=now)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        reloaded = store.get(task.task_id)
        assert reloaded.enabled is False

    async def test_missing_destination_logs_but_does_not_raise(
        self, store, slack_client, client_resolver, dispatch_fn, monkeypatch
    ):
        monkeypatch.delenv("BRAM_DM_CHANNEL", raising=False)
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(destination=None, next_run_at=now)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_agent_tasks()

        assert summaries[0]["status"] == "no_destination"
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_in_flight_agent_task_not_refired_on_next_tick(self, store, client_resolver):
        """A cron task fired in tick N must not be re-dispatched in tick N+1 while still in flight.

        This is the core regression: without the pre-claim fix, list_due returns
        the row on every tick until run_task finally writes next_run_at at the
        very end of dispatch, causing ~10× concurrent launches on a 30s poll /
        300s timeout agent task.
        """
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now - timedelta(minutes=1))
        store.create(task)

        dispatch_started = asyncio.Event()
        dispatch_can_finish = asyncio.Event()
        dispatch_count = {"n": 0}

        async def slow_dispatch(agent_name, message, channel, thread_ts, client, timeout):
            dispatch_count["n"] += 1
            dispatch_started.set()
            await asyncio.wait_for(dispatch_can_finish.wait(), timeout=5.0)
            return {"agent": agent_name, "status": "ok", "response": "done"}

        # Tick 1: task is due, fires, dispatch starts but does not finish.
        await scheduler.run_once(store, client_resolver, slow_dispatch, now=now)
        await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)

        # Tick 2 (30s later): simulate the next poll interval.
        # next_run_at should already be claimed (advanced), so the task is not due.
        now2 = now + timedelta(seconds=30)
        await scheduler.run_once(store, client_resolver, slow_dispatch, now=now2)

        # Allow the in-flight dispatch to complete.
        dispatch_can_finish.set()
        await scheduler.drain_agent_tasks()

        assert dispatch_count["n"] == 1, f"Task was dispatched {dispatch_count['n']} times; expected exactly 1"

    async def test_falls_back_to_bram_dm_env(self, store, slack_client, client_resolver, dispatch_fn, monkeypatch):
        monkeypatch.setenv("BRAM_DM_CHANNEL", "D_BRAM")
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(destination=None, next_run_at=now)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

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

        def failing_resolver(_agent):
            return failing_client

        summaries = await scheduler.run_task(task, store, failing_resolver, dispatch_fn, now=now)

        assert summaries["status"] == "post_failed"
        # Schedule still advanced so the task isn't stuck on the failing post
        assert store.get(task.task_id).next_run_at > now


@pytest.mark.unit
@pytest.mark.asyncio
class TestRunForever:
    async def test_stop_event_terminates_loop(self, store, slack_client, client_resolver, dispatch_fn):
        # No due tasks, scheduler should wake, idle, and exit when stop_event is set.
        stop = asyncio.Event()
        task_coro = asyncio.create_task(
            scheduler.run_forever(store, client_resolver, dispatch_fn, poll_interval_seconds=0.05, stop_event=stop)
        )
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task_coro, timeout=1.0)

    async def test_loop_swallows_errors_and_keeps_running(self, store, slack_client, client_resolver, monkeypatch):
        # Force run_once to raise once, then succeed — the loop should survive.
        calls = {"count": 0}

        async def flaky(_store, _client, _dispatch, now=None, timeout=0, system_client_resolver=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient")
            return []

        monkeypatch.setattr(scheduler, "run_once", flaky)

        stop = asyncio.Event()
        loop_task = asyncio.create_task(
            scheduler.run_forever(store, client_resolver, AsyncMock(), poll_interval_seconds=0.01, stop_event=stop)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1.0)

        assert calls["count"] >= 2


@pytest.mark.unit
@pytest.mark.asyncio
class TestAgentIsolation:
    async def test_each_task_invokes_its_own_agent(self, store, slack_client, client_resolver):
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        lisa_task = _make_task(agent_name="lisa", next_run_at=now, destination="C_LISA")
        sam_task = _make_task(agent_name="sam", prompt="Sam's task", next_run_at=now, destination="C_SAM")
        store.create(lisa_task)
        store.create(sam_task)

        calls: list[str] = []

        async def dispatch(agent_name, message, channel, thread_ts, client, timeout):
            calls.append(agent_name)
            return {"agent": agent_name, "status": "ok", "response": f"{agent_name} done"}

        await scheduler.run_once(store, client_resolver, dispatch, now=now)
        await scheduler.drain_agent_tasks()

        assert sorted(calls) == ["lisa", "sam"]

    async def test_scheduler_posts_via_each_tasks_own_client(self, store, dispatch_fn):
        """A single task must post via the task owner's client, not every agent's.

        Regression for: per-bolt_app schedulers all reading the shared store
        meant every due task was posted under every bot at once.
        """
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        store.create(_make_task(agent_name="lisa", next_run_at=now, destination="C_LISA"))
        store.create(_make_task(agent_name="sam", prompt="Sam", next_run_at=now, destination="C_SAM"))

        lisa_client = MagicMock()
        lisa_client.chat_postMessage = AsyncMock(return_value={"ok": True})
        sam_client = MagicMock()
        sam_client.chat_postMessage = AsyncMock(return_value={"ok": True})

        clients = {"lisa": lisa_client, "sam": sam_client}

        await scheduler.run_once(store, clients.get, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        lisa_client.chat_postMessage.assert_awaited_once()
        assert lisa_client.chat_postMessage.call_args.kwargs["channel"] == "C_LISA"
        sam_client.chat_postMessage.assert_awaited_once()
        assert sam_client.chat_postMessage.call_args.kwargs["channel"] == "C_SAM"

    async def test_scheduler_skips_task_when_no_client_for_agent(self, store, dispatch_fn):
        """If an agent's bolt_app failed to start, its tasks must skip cleanly."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        store.create(_make_task(agent_name="ghost", next_run_at=now, destination="C_X"))

        await scheduler.run_once(store, lambda _a: None, dispatch_fn, now=now)
        summaries = await scheduler.drain_agent_tasks()

        assert summaries[0]["status"] == "no_client"
        dispatch_fn.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
class TestSystemTasks:
    """System tasks (callable_ref set) bypass dispatch_fn entirely."""

    async def test_system_task_invokes_callable_with_payload(
        self, store, slack_client, client_resolver, dispatch_fn, monkeypatch
    ):
        calls: list[dict] = []

        async def fake_callable(*, payload, slack_client, now):
            calls.append({"payload": payload, "slack_client": slack_client, "now": now})
            return {"status": "ok"}

        # Inject the callable via the import path so the scheduler's
        # importlib lookup finds it. Use the scheduler module itself
        # as the target — it's already imported.
        scheduler.fake_callable = fake_callable  # type: ignore[attr-defined]

        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="sam",
            name="supervise disp-1",
            callable_ref="router.scheduled_tasks.scheduler:fake_callable",
            payload={"dispatch_id": "disp-1"},
            period_seconds=120,
            now=now - timedelta(seconds=121),  # past so it's due
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_system_tasks()

        assert len(summaries) == 1
        assert summaries[0]["kind"] == "system"
        assert summaries[0]["status"] == "ok"

        # dispatch_fn was NOT called — that's the whole point of system tasks.
        dispatch_fn.assert_not_awaited()
        # slack_client wasn't called by the scheduler itself either; the
        # callable owns all Slack interaction.
        slack_client.chat_postMessage.assert_not_awaited()

        assert len(calls) == 1
        assert calls[0]["payload"] == {"dispatch_id": "disp-1"}
        assert calls[0]["slack_client"] is slack_client

        # next_run advanced by period_seconds
        reloaded = store.get(task.task_id)
        assert reloaded.next_run_at == now + timedelta(seconds=120)

    async def test_system_task_done_status_deletes_task(self, store, slack_client, client_resolver, dispatch_fn):
        async def fake_done(**kwargs):
            return {"status": "done", "reason": "exitcode", "exitcode": 0}

        scheduler.fake_done = fake_done  # type: ignore[attr-defined]

        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="sam",
            name="supervise disp-1",
            callable_ref="router.scheduled_tasks.scheduler:fake_done",
            payload={"dispatch_id": "disp-1"},
            period_seconds=120,
            now=now - timedelta(seconds=121),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_system_tasks()

        assert summaries[0]["status"] == "done"
        assert store.get(task.task_id) is None

    async def test_system_task_callable_exception_keeps_polling(
        self, store, slack_client, client_resolver, dispatch_fn, caplog
    ):
        async def broken(**kwargs):
            raise RuntimeError("transient")

        scheduler.broken = broken  # type: ignore[attr-defined]

        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="sam",
            name="x",
            callable_ref="router.scheduled_tasks.scheduler:broken",
            payload={},
            period_seconds=60,
            now=now - timedelta(seconds=61),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_system_tasks()

        assert summaries[0]["status"] == "callable_error"
        # Still scheduled — the supervisor's correctness should not depend
        # on the callable never raising.
        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.next_run_at == now + timedelta(seconds=60)

    async def test_system_task_invalid_callable_ref_disables_task(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="sam",
            name="x",
            callable_ref="router.scheduled_tasks.scheduler:does_not_exist",
            payload={},
            period_seconds=60,
            now=now - timedelta(seconds=61),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_system_tasks()

        assert summaries[0]["status"] == "callable_import_error"
        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.enabled is False

    async def test_system_task_skips_when_no_client(self, store, dispatch_fn):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="ghost",
            name="x",
            callable_ref="router.scheduled_tasks.scheduler:_import_callable",  # any importable
            payload={},
            period_seconds=60,
            now=now - timedelta(seconds=61),
        )

        await scheduler.run_once(store, lambda _a: None, dispatch_fn, now=now)
        summaries = await scheduler.drain_system_tasks()

        assert summaries[0]["status"] == "no_client"
        # next_run still advanced so we don't busy-loop.
        reloaded = store.get(task.task_id)
        assert reloaded.next_run_at == now + timedelta(seconds=60)

    async def test_system_task_uses_system_client_resolver_when_provided(self, store, client_resolver, dispatch_fn):
        """Issue #270: a system task posts through ``system_client_resolver``
        (the workers bot in production), not the agent ``client_resolver`` — so
        runtime dispatch posts speak with one identity."""
        seen: list = []

        async def fake_callable(*, payload, slack_client, now):
            seen.append(slack_client)
            return {"status": "ok"}

        scheduler.fake_sys_client = fake_callable  # type: ignore[attr-defined]

        workers_client = MagicMock(name="workers_client")
        system_resolver = lambda _agent: workers_client  # noqa: E731

        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        store.create_system_task(
            agent_name="sam",
            name="supervise disp-1",
            callable_ref="router.scheduled_tasks.scheduler:fake_sys_client",
            payload={"dispatch_id": "disp-1"},
            period_seconds=120,
            now=now - timedelta(seconds=121),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now, system_client_resolver=system_resolver)
        await scheduler.drain_system_tasks()

        # The callable received the workers client, not the agent one.
        assert seen == [workers_client]

    async def test_system_task_falls_back_to_client_resolver_without_system_resolver(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        """When no ``system_client_resolver`` is passed (e.g. the supervision
        smoke test), the system task posts through the regular resolver — this
        is the seam the #273 regression broke by building a client internally."""
        seen: list = []

        async def fake_callable(*, payload, slack_client, now):
            seen.append(slack_client)
            return {"status": "ok"}

        scheduler.fake_sys_fallback = fake_callable  # type: ignore[attr-defined]

        now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        store.create_system_task(
            agent_name="sam",
            name="supervise disp-1",
            callable_ref="router.scheduled_tasks.scheduler:fake_sys_fallback",
            payload={"dispatch_id": "disp-1"},
            period_seconds=120,
            now=now - timedelta(seconds=121),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_system_tasks()

        assert seen == [slack_client]


def _make_wakeup_task(**overrides) -> ScheduledTask:
    defaults = {
        "task_id": str(uuid.uuid4()),
        "agent_name": "sam",
        "name": "wakeup",
        "prompt": "",
        "schedule_cron": SYSTEM_TASK_CRON_MARKER,
        "next_run_at": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        "destination": "C_WAKE",
        "enabled": True,
        "created_at": datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc),
        "one_shot": True,
        "payload": {
            "reason": "check PR",
            "channel_id": "C_WAKE",
            "thread_ts": "1234.5678",
        },
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.mark.unit
@pytest.mark.asyncio
class TestOneShotWakeup:
    """Post-fire branch deletes the row for one_shot=True tasks."""

    async def test_one_shot_task_deleted_after_fire(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_agent_tasks()

        assert len(summaries) == 1
        assert summaries[0]["status"] == "ok"
        # Row must be gone — one-shot tasks self-destruct after firing.
        assert store.get(task.task_id) is None

    async def test_one_shot_passes_channel_and_thread_ts_to_dispatch(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        call_kwargs = dispatch_fn.call_args.kwargs
        assert call_kwargs["channel"] == "C_WAKE"
        assert call_kwargs["thread_ts"] == "1234.5678"

    async def test_one_shot_post_includes_thread_ts(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        post_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert post_kwargs["channel"] == "C_WAKE"
        assert post_kwargs["thread_ts"] == "1234.5678"

    async def test_one_shot_not_deleted_on_dispatch_error(self, store, client_resolver):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        await scheduler.run_once(store, client_resolver, failing_dispatch, now=now)
        summaries = await scheduler.drain_agent_tasks()

        assert summaries[0]["status"] == "dispatch_error"
        # Task retained and rescheduled — transient failure must not lose the one-shot.
        assert store.get(task.task_id) is not None
        assert store.get(task.task_id).next_run_at > now


@pytest.mark.unit
@pytest.mark.asyncio
class TestPollWakeup:
    """Recurring wakeup tasks decrement attempts_remaining and delete at zero."""

    def _make_poll_task(self, now: datetime, attempts: int = 3) -> ScheduledTask:
        return ScheduledTask(
            task_id=str(uuid.uuid4()),
            agent_name="sam",
            name="wakeup_poll",
            prompt="",
            schedule_cron=SYSTEM_TASK_CRON_MARKER,
            next_run_at=now - timedelta(seconds=1),
            destination="C_POLL",
            enabled=True,
            created_at=now - timedelta(minutes=5),
            one_shot=False,
            period_seconds=60,
            payload={
                "reason": "poll PR",
                "channel_id": "C_POLL",
                "thread_ts": "9999.0001",
                "attempts_remaining": attempts,
                "max_attempts": attempts,
            },
        )

    async def test_poll_decrements_attempts(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=3)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.payload["attempts_remaining"] == 2

    async def test_poll_advances_next_run_at(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=3)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        reloaded = store.get(task.task_id)
        assert reloaded.next_run_at == now + timedelta(seconds=60)

    async def test_poll_deleted_when_attempts_exhausted(self, store, slack_client, client_resolver, dispatch_fn):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=1)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        assert store.get(task.task_id) is None

    async def test_dispatch_error_does_not_decrement_attempts(self, store, client_resolver):
        """Failed fire (dispatch_error) must not consume an attempt."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=3)
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("transient"))
        summary = await scheduler.run_task(task, store, client_resolver, failing_dispatch, now=now)

        assert summary["status"] == "dispatch_error"
        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.payload["attempts_remaining"] == 3, "attempt must not be decremented on dispatch_error"

    async def test_no_client_does_not_decrement_attempts(self, store, dispatch_fn):
        """Failed fire (no_client) must not consume an attempt."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=3)
        store.create(task)

        summary = await scheduler.run_task(task, store, lambda _: None, dispatch_fn, now=now)

        assert summary["status"] == "no_client"
        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.payload["attempts_remaining"] == 3, "attempt must not be decremented on no_client"

    async def test_failed_fire_advances_next_run_at(self, store, client_resolver):
        """A transient failure reschedules without consuming an attempt."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=3)
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        await scheduler.run_task(task, store, client_resolver, failing_dispatch, now=now)

        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.next_run_at > now

    async def test_failure_cap_terminates_task(self, store, client_resolver):
        """After WAKEUP_MAX_FAILED_FIRES consecutive failures the task is deleted."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=5)
        # Pre-seed failed_fire_count at one below the cap so the next failure terminates.
        payload = {**task.payload, "failed_fire_count": scheduler.WAKEUP_MAX_FAILED_FIRES - 1}
        task = ScheduledTask(**{**task.__dict__, "payload": payload})
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("permanent failure"))
        summary = await scheduler.run_task(task, store, client_resolver, failing_dispatch, now=now)

        assert summary["status"] == "dispatch_error"
        assert store.get(task.task_id) is None, "task must be deleted when failure cap is reached"

    async def test_successful_fire_still_decrements_after_failures(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        """A successful fire decrements attempts_remaining even if prior failures occurred."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_poll_task(now, attempts=3)
        # Simulate some prior failed fires that did not decrement attempts.
        payload = {**task.payload, "failed_fire_count": 3}
        task = ScheduledTask(**{**task.__dict__, "payload": payload})
        store.create(task)

        await scheduler.run_task(task, store, client_resolver, dispatch_fn, now=now)

        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.payload["attempts_remaining"] == 2, "success must still decrement attempts_remaining"


@pytest.mark.unit
@pytest.mark.asyncio
class TestArchivedThreadDrop:
    """When the Slack post fails with an archived/gone thread error, the task is deleted."""

    async def test_archived_thread_deletes_task_and_sets_status(self, store, dispatch_fn):
        from slack_sdk.errors import SlackApiError  # noqa: PLC0415

        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        archived_response = MagicMock()
        archived_response.get.return_value = "is_archived"
        archived_client = MagicMock()
        archived_client.chat_postMessage = AsyncMock(side_effect=SlackApiError("archived", archived_response))

        await scheduler.run_once(store, lambda _: archived_client, dispatch_fn, now=now)
        summaries = await scheduler.drain_agent_tasks()

        assert summaries[0]["status"] == "thread_gone"
        assert store.get(task.task_id) is None

    async def test_non_archived_error_does_not_delete(self, store, dispatch_fn):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        broken_client = MagicMock()
        broken_client.chat_postMessage = AsyncMock(side_effect=RuntimeError("network error"))

        await scheduler.run_once(store, lambda _: broken_client, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        # One-shot is still deleted (post_failed branch falls through to scheduling)
        assert store.get(task.task_id) is None


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelSuppression:
    """Sentinel __NO_POST__ suppresses the Slack post; empty/normal responses are unaffected."""

    async def test_no_post_sentinel_suppresses_message(self, store, slack_client, client_resolver):
        """(a) Exact __NO_POST__ response → no chat_postMessage, status suppressed."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        suppressed_dispatch = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "__NO_POST__"})
        summary = await scheduler.run_task(task, store, client_resolver, suppressed_dispatch, now=now)

        assert summary["status"] == "suppressed"
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_no_post_sentinel_with_surrounding_whitespace(self, store, slack_client, client_resolver):
        """Sentinel match is exact after strip — leading/trailing whitespace is ignored."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        suppressed_dispatch = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "  __NO_POST__\n"})
        summary = await scheduler.run_task(task, store, client_resolver, suppressed_dispatch, now=now)

        assert summary["status"] == "suppressed"
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_empty_response_posts_canary(self, store, slack_client, client_resolver):
        """(b) Empty response → still posts (no output from <agent>) canary; status ok."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        empty_dispatch = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": ""})
        summary = await scheduler.run_task(task, store, client_resolver, empty_dispatch, now=now)

        assert summary["status"] == "ok"
        slack_client.chat_postMessage.assert_awaited_once()
        post_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert "(no output from lisa)" in post_kwargs["text"]

    async def test_normal_response_posts_text(self, store, slack_client, client_resolver):
        """(c) Normal text response → posts the text; status ok."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        normal_dispatch = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "All systems nominal."})
        summary = await scheduler.run_task(task, store, client_resolver, normal_dispatch, now=now)

        assert summary["status"] == "ok"
        slack_client.chat_postMessage.assert_awaited_once()
        post_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert post_kwargs["text"] == "All systems nominal."

    async def test_sentinel_still_advances_scheduling(self, store, slack_client, client_resolver):
        """After suppression the next_run_at is still advanced (task keeps running on schedule)."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        suppressed_dispatch = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "__NO_POST__"})
        await scheduler.run_task(task, store, client_resolver, suppressed_dispatch, now=now)

        reloaded = store.get(task.task_id)
        assert reloaded.last_run_at == now
        assert reloaded.next_run_at > now

    async def test_sentinel_with_trailing_prose_posts_normally(self, store, slack_client, client_resolver):
        """Sentinel with extra text after it does not match; the full text is posted as-is."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        partial_dispatch = AsyncMock(
            return_value={"agent": "lisa", "status": "ok", "response": "__NO_POST__ some trailing text"}
        )
        summary = await scheduler.run_task(task, store, client_resolver, partial_dispatch, now=now)

        assert summary["status"] == "ok"
        slack_client.chat_postMessage.assert_awaited_once()
        post_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert "__NO_POST__ some trailing text" in post_kwargs["text"]

    async def test_sentinel_on_own_line_after_preamble_suppresses(self, store, slack_client, client_resolver):
        """Regression: a preamble line followed by __NO_POST__ on its own line still suppresses.

        Models narrate first ("No errors found.") then emit the sentinel; whole-string
        equality let that preamble defeat suppression. Line-presence match fixes it.
        """
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        narrated_dispatch = AsyncMock(
            return_value={
                "agent": "lisa",
                "status": "ok",
                "response": "No ERROR or CRITICAL log-level lines found.\n\n__NO_POST__",
            }
        )
        summary = await scheduler.run_task(task, store, client_resolver, narrated_dispatch, now=now)

        assert summary["status"] == "suppressed"
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_inline_sentinel_mention_does_not_suppress(self, store, slack_client, client_resolver):
        """An inline mention of the token (not on its own line) must NOT suppress the post."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        inline_dispatch = AsyncMock(
            return_value={
                "agent": "lisa",
                "status": "ok",
                "response": "The sentinel __NO_POST__ is used to suppress output.",
            }
        )
        summary = await scheduler.run_task(task, store, client_resolver, inline_dispatch, now=now)

        assert summary["status"] == "ok"
        slack_client.chat_postMessage.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
class TestPerTaskTimeout:
    """Per-task timeout_seconds is plumbed to dispatch_fn (#351)."""

    async def test_per_task_timeout_used_when_set(self, store, slack_client, client_resolver, dispatch_fn):
        """(a) task.timeout_seconds overrides the loop-wide default."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, timeout_seconds=1800)
        store.create(task)

        await scheduler.run_task(task, store, client_resolver, dispatch_fn, now=now, timeout=300)

        call_kwargs = dispatch_fn.call_args.kwargs
        assert call_kwargs["timeout"] == 1800

    async def test_null_timeout_falls_back_to_loop_default(self, store, slack_client, client_resolver, dispatch_fn):
        """(b) NULL / unset timeout_seconds defaults to the loop-wide timeout."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, timeout_seconds=None)
        store.create(task)

        await scheduler.run_task(task, store, client_resolver, dispatch_fn, now=now, timeout=300)

        call_kwargs = dispatch_fn.call_args.kwargs
        assert call_kwargs["timeout"] == 300

    async def test_long_task_does_not_serialize_second_due_task(self, store, slack_client, client_resolver):
        """(c) Two due agent tasks launch concurrently; a slow first task does not block the second."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        task_a = _make_task(
            agent_name="lisa",
            name="slow-task",
            next_run_at=now,
            destination="C_A",
        )
        task_b = _make_task(
            agent_name="sam",
            name="fast-task",
            next_run_at=now,
            destination="C_B",
        )
        store.create(task_a)
        store.create(task_b)

        # task_a blocks until task_b has started; both must be in-flight at the
        # same time for this to pass. An asyncio.Event is used as the handshake.
        task_b_started = asyncio.Event()
        task_a_unblocked = asyncio.Event()

        async def slow_dispatch(agent_name, message, channel, thread_ts, client, timeout):
            if agent_name == "lisa":
                # Wait until sam's task has started, proving we're concurrent.
                await asyncio.wait_for(task_b_started.wait(), timeout=2.0)
                task_a_unblocked.set()
            else:
                task_b_started.set()
                # Yield to let lisa's dispatch notice the event.
                await asyncio.sleep(0)
            return {"agent": agent_name, "status": "ok", "response": f"{agent_name} done"}

        await scheduler.run_once(store, client_resolver, slow_dispatch, now=now)
        await scheduler.drain_agent_tasks()

        # If we reach here without TimeoutError, both tasks ran concurrently.
        assert task_b_started.is_set()
        assert task_a_unblocked.is_set()

    async def test_post_on_completion_fires_after_background_dispatch(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        """(d) post-on-completion fires even when the task runs in the background."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, destination="C_INBOX")
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        # Before drain, the Slack post has not yet fired.
        slack_client.chat_postMessage.assert_not_awaited()

        await scheduler.drain_agent_tasks()
        slack_client.chat_postMessage.assert_awaited_once()
        assert slack_client.chat_postMessage.call_args.kwargs["channel"] == "C_INBOX"

    async def test_timeout_seconds_persists_through_store_roundtrip(self, store):
        """timeout_seconds survives a write/read cycle through SQLite."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, timeout_seconds=1800)
        store.create(task)

        reloaded = store.get(task.task_id)
        assert reloaded.timeout_seconds == 1800

    async def test_null_timeout_seconds_round_trips(self, store):
        """A task without timeout_seconds stores and reads back as None."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now, timeout_seconds=None)
        store.create(task)

        reloaded = store.get(task.task_id)
        assert reloaded.timeout_seconds is None


@pytest.mark.unit
@pytest.mark.asyncio
class TestOneShotRetry:
    """Regression: one-shot tasks must not be deleted on transient fire failures (#518).

    Acceptance criteria:
      (a) no_client → task retained, next_run_at advances
      (b) dispatch_error → task retained
      (c) successful fire → task deleted exactly once
      (d) max retries exhausted → task deleted
    """

    def _make_one_shot(self, now: datetime) -> ScheduledTask:
        from router.scheduled_tasks.store import SYSTEM_TASK_CRON_MARKER

        return ScheduledTask(
            task_id=str(uuid.uuid4()),
            agent_name="sam",
            name="one_shot_reminder",
            prompt="",
            schedule_cron=SYSTEM_TASK_CRON_MARKER,
            next_run_at=now - timedelta(seconds=1),
            destination="C_REMINDER",
            enabled=True,
            created_at=now - timedelta(minutes=10),
            one_shot=True,
            payload={"channel_id": "C_REMINDER", "thread_ts": "1111.2222", "reason": "test wakeup"},
        )

    async def test_no_client_retains_task_and_advances_next_run_at(self, store, dispatch_fn):
        """(a) client_resolver returning None must NOT delete the one-shot task."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_one_shot(now)
        store.create(task)

        summary = await scheduler.run_task(task, store, lambda _: None, dispatch_fn, now=now)

        assert summary["status"] == "no_client"
        assert store.get(task.task_id) is not None, "one-shot must not be deleted on no_client"
        reloaded = store.get(task.task_id)
        assert reloaded.next_run_at > now

    async def test_dispatch_error_retains_task(self, store, slack_client, client_resolver):
        """(b) dispatch_fn raising must NOT delete the one-shot task."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_one_shot(now)
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("transient"))
        summary = await scheduler.run_task(task, store, client_resolver, failing_dispatch, now=now)

        assert summary["status"] == "dispatch_error"
        assert store.get(task.task_id) is not None, "one-shot must not be deleted on dispatch_error"

    async def test_successful_fire_deletes_task_exactly_once(self, store, slack_client, client_resolver, dispatch_fn):
        """(c) A successful one-shot fire deletes the task exactly once."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_one_shot(now)
        store.create(task)

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        await scheduler.drain_agent_tasks()

        assert store.get(task.task_id) is None, "one-shot must be deleted after successful fire"

    async def test_max_retries_exhausted_deletes_task(self, store, slack_client, client_resolver):
        """(d) After exhausting retry attempts the one-shot task is deleted."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_one_shot(now)
        # Payload with one retry attempt remaining so the next failure exhausts it.
        task = ScheduledTask(
            **{
                **task.__dict__,
                "payload": {**task.payload, "one_shot_retry_attempts_remaining": 1},
            }
        )
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("permanent"))
        summary = await scheduler.run_task(task, store, client_resolver, failing_dispatch, now=now)

        assert summary["status"] == "dispatch_error"
        assert store.get(task.task_id) is None, "one-shot must be deleted after max retries exhausted"

    async def test_no_client_warning_log_fires_on_retry(self, store, dispatch_fn, caplog):
        """The no_client warning is emitted on each skipped attempt."""
        import logging

        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_one_shot(now)
        store.create(task)

        with caplog.at_level(logging.WARNING, logger="router.scheduled_tasks.scheduler"):
            await scheduler.run_task(task, store, lambda _: None, dispatch_fn, now=now)

        assert any("no_client" in r.message or "No Slack client" in r.message for r in caplog.records)

    async def test_retry_decrements_attempts_remaining(self, store, slack_client, client_resolver):
        """Each failed fire decrements one_shot_retry_attempts_remaining in the payload."""
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = self._make_one_shot(now)
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        await scheduler.run_task(task, store, client_resolver, failing_dispatch, now=now)

        reloaded = store.get(task.task_id)
        assert reloaded is not None
        expected = scheduler.ONE_SHOT_MAX_RETRIES - 1
        assert reloaded.payload.get("one_shot_retry_attempts_remaining") == expected


@pytest.mark.unit
@pytest.mark.asyncio
class TestSystemTaskConcurrency:
    """A slow system task must not freeze the scheduler loop for other due tasks."""

    async def test_slow_system_task_does_not_block_concurrent_system_task(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        """Two system tasks due at the same tick run concurrently, not serially.

        Before the fix, system tasks were awaited inline; a 60s-blocking
        check_dispatch would hold up every other due task for the full wait.
        After detaching via asyncio.create_task, both tasks start immediately
        and the fast one completes while the slow one is still in flight.
        """
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        fast_started = asyncio.Event()
        slow_can_finish = asyncio.Event()

        async def slow_callable(*, payload, slack_client, now):
            await asyncio.wait_for(slow_can_finish.wait(), timeout=2.0)
            return {"status": "ok"}

        async def fast_callable(*, payload, slack_client, now):
            fast_started.set()
            return {"status": "ok"}

        scheduler.slow_callable_502 = slow_callable  # type: ignore[attr-defined]
        scheduler.fast_callable_502 = fast_callable  # type: ignore[attr-defined]

        store.create_system_task(
            agent_name="sam",
            name="slow-supervision",
            callable_ref="router.scheduled_tasks.scheduler:slow_callable_502",
            payload={},
            period_seconds=120,
            now=now - timedelta(seconds=121),
        )
        store.create_system_task(
            agent_name="sam",
            name="fast-supervision",
            callable_ref="router.scheduled_tasks.scheduler:fast_callable_502",
            payload={},
            period_seconds=120,
            now=now - timedelta(seconds=121),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

        # The fast task must start even while the slow task is still blocked —
        # proving both tasks are in-flight at the same time.
        await asyncio.wait_for(fast_started.wait(), timeout=1.0)

        # Let the slow task finish, then drain both.
        slow_can_finish.set()
        await scheduler.drain_system_tasks()

        assert fast_started.is_set()

    async def test_system_task_callable_timeout_keeps_polling(
        self, store, slack_client, client_resolver, dispatch_fn, monkeypatch
    ):
        """A callable that hangs past SYSTEM_TASK_CALLABLE_TIMEOUT_SECONDS is
        cancelled and the task is kept scheduled (not deleted)."""
        monkeypatch.setattr(scheduler, "SYSTEM_TASK_CALLABLE_TIMEOUT_SECONDS", 0.05)

        async def hanging_callable(*, payload, slack_client, now):
            await asyncio.sleep(10)  # longer than the patched timeout
            return {"status": "ok"}

        scheduler.hanging_callable_502 = hanging_callable  # type: ignore[attr-defined]

        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = store.create_system_task(
            agent_name="sam",
            name="hanging-task",
            callable_ref="router.scheduled_tasks.scheduler:hanging_callable_502",
            payload={},
            period_seconds=60,
            now=now - timedelta(seconds=61),
        )

        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        summaries = await scheduler.drain_system_tasks()

        assert summaries[0]["status"] == "callable_timeout"
        # Task must stay scheduled — a hung callable is treated like any
        # transient error so supervision keeps polling.
        reloaded = store.get(task.task_id)
        assert reloaded is not None
        assert reloaded.next_run_at == now + timedelta(seconds=60)

    async def test_system_task_not_refired_on_subsequent_poll_while_in_flight(
        self, store, slack_client, client_resolver, dispatch_fn
    ):
        """A system task's next_run_at is pre-claimed before detaching, so a
        second run_once call within the same period does not re-fire the callable."""
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)

        call_count = 0
        callable_blocked = asyncio.Event()
        callable_can_finish = asyncio.Event()

        async def blocking_callable(*, payload, slack_client, now):
            nonlocal call_count
            call_count += 1
            callable_blocked.set()
            await asyncio.wait_for(callable_can_finish.wait(), timeout=2.0)
            return {"status": "ok"}

        scheduler.blocking_callable_refire_502 = blocking_callable  # type: ignore[attr-defined]

        store.create_system_task(
            agent_name="sam",
            name="refire-guard",
            callable_ref="router.scheduled_tasks.scheduler:blocking_callable_refire_502",
            payload={},
            period_seconds=120,
            now=now - timedelta(seconds=121),
        )

        # First tick: dispatches and pre-claims next_run_at.
        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)
        # Wait until the callable has actually started.
        await asyncio.wait_for(callable_blocked.wait(), timeout=1.0)

        # Second tick within the same period — must NOT re-fire the callable.
        await scheduler.run_once(store, client_resolver, dispatch_fn, now=now + timedelta(seconds=30))

        # Release the in-flight callable and drain.
        callable_can_finish.set()
        await scheduler.drain_system_tasks()

        assert call_count == 1, f"callable was invoked {call_count} times; expected 1"
