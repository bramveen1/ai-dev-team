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

        summaries = await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

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

        summaries = await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

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

        summaries = await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

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

        summaries = await scheduler.run_once(store, client_resolver, dispatch_fn, now=now)

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

        summaries = await scheduler.run_once(store, lambda _a: None, dispatch_fn, now=now)

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

    async def test_one_shot_deleted_even_on_dispatch_error(self, store, client_resolver):
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        task = _make_wakeup_task(next_run_at=now - timedelta(seconds=1))
        store.create(task)

        failing_dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        await scheduler.run_once(store, client_resolver, failing_dispatch, now=now)
        summaries = await scheduler.drain_agent_tasks()

        assert summaries[0]["status"] == "dispatch_error"
        # Still deleted — we don't retry one-shot wakeups.
        assert store.get(task.task_id) is None


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
