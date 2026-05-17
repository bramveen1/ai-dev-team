"""Unit tests for router.dispatch.supervision — the polling check_dispatch.

Covers every detection path from the issue's AC matrix: terminal,
halted, timeout, orphan, delta, quiet. Also exercises the
``mark_halted_for_agent`` helper used by ``/kill``.

State files are written into a tmp_path fixture and supervision is
invoked with ``dispatch_root=tmp_path``; the real ``/var/lib/dispatch``
volume is never touched.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import state as dstate
from router.dispatch import supervision

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


@pytest.fixture(autouse=True)
def reset_delta_cache():
    supervision.reset_delta_cache()
    yield
    supervision.reset_delta_cache()


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


def _seed_dispatch(
    root: str,
    *,
    dispatch_id: str = "disp-1",
    pid: int = 0,
    started_at: datetime | None = None,
    budget: int = 1800,
    channel: str = "C123",
    thread_ts: str = "1.0",
    agent: str = "sam",
) -> None:
    if pid:
        dstate.write_field(dispatch_id, dstate.FIELD_PID, str(pid), root=root)
    dstate.write_field(
        dispatch_id,
        dstate.FIELD_STARTED_AT,
        (started_at or datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)).isoformat(),
        root=root,
    )
    dstate.write_field(dispatch_id, dstate.FIELD_BUDGET, str(budget), root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_CHANNEL, channel, root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_THREAD_TS, thread_ts, root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_AGENT, agent, root=root)


def _payload(
    *,
    dispatch_id: str = "disp-1",
    channel: str = "C123",
    thread_ts: str = "1.0",
    agent: str = "sam",
) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "agent": agent,
    }


@pytest.mark.asyncio
class TestTerminal:
    async def test_exitcode_zero_posts_success_and_deregisters(self, root, slack_client):
        _seed_dispatch(root, pid=os.getpid())
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_COST, "0.42", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 0}
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":white_check_mark:" in text
        assert "disp-1" in text
        assert "$0.42" in text
        assert "/pull/9" in text
        assert "<@sam>" in text

    async def test_nonzero_exitcode_posts_failure(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "2", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result["status"] == "done"
        assert result["exitcode"] == 2
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":x:" in text
        assert "exit 2" in text

    async def test_unparseable_exitcode_treated_as_synthetic(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "garbage", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result["exitcode"] == -1
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":warning:" in text


@pytest.mark.asyncio
class TestHalt:
    async def test_halt_marker_sigterms_and_synthesizes_exitcode(self, root, slack_client, monkeypatch):
        sigterm_calls: list[str] = []
        monkeypatch.setattr(supervision, "_send_sigterm", lambda pid: sigterm_calls.append(pid) or True)

        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "killed"}
        assert sigterm_calls == ["12345"]
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":octagonal_sign:" in text
        assert "killed" in text


@pytest.mark.asyncio
class TestTimeout:
    async def test_exceeded_budget_kills_and_synthesizes(self, root, slack_client, monkeypatch):
        sigterm_calls: list[str] = []
        monkeypatch.setattr(supervision, "_send_sigterm", lambda pid: sigterm_calls.append(pid) or True)

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)

        now = started + timedelta(seconds=120)
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=now,
        )

        assert result == {"status": "done", "reason": "timeout"}
        assert sigterm_calls == ["5555"]
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":alarm_clock:" in text
        assert "timed out" in text

    async def test_within_budget_keeps_polling(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started, budget=600)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=120),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
class TestOrphan:
    async def test_dead_pid_no_exitcode_synthesizes(self, root, slack_client):
        # 2**30 is well past any plausible live pid on a normal system.
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=2**30, started_at=started)
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "done", "reason": "orphan"}
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":ghost:" in text
        assert "orphan" in text


@pytest.mark.asyncio
class TestDelta:
    async def test_first_observation_posts_delta(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_TOOL, "Edit", root=root)
        dstate.write_field("disp-1", dstate.FIELD_COST, "0.10", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "event: assistant" in text
        assert "tool: Edit" in text
        assert "$0.10" in text

    async def test_unchanged_state_emits_nothing(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)

        # First tick: posts the delta.
        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )
        slack_client.chat_postMessage.reset_mock()

        # Second tick with no field change: silent.
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=60),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_only_changed_fields_appear_in_delta(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_COST, "0.10", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )
        slack_client.chat_postMessage.reset_mock()

        # Only cost changes.
        dstate.write_field("disp-1", dstate.FIELD_COST, "0.20", root=root)
        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=60),
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "cost: $0.20" in text
        assert "event:" not in text


@pytest.mark.asyncio
class TestQuiet:
    async def test_no_state_no_post(self, root, slack_client):
        # Bare-bones — only the handler-written init files, no progress.
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
class TestSlackResilience:
    async def test_slack_post_failure_does_not_raise(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        slack_client.chat_postMessage.side_effect = RuntimeError("slack down")

        # Must still return done so the scheduler deregisters.
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result["status"] == "done"


class TestRegisterSupervision:
    def test_creates_system_task_with_canonical_callable_ref(self, tmp_path):
        from router.scheduled_tasks.store import ScheduledTaskStore

        store = ScheduledTaskStore(str(tmp_path / "tasks.db"))
        try:
            task = supervision.register_supervision(
                store,
                dispatch_id="disp-x",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                period_seconds=60,
            )
            assert task.callable_ref == supervision.CALLABLE_REF
            assert task.agent_name == "sam"
            assert task.destination == "C1"
            assert task.period_seconds == 60
            assert task.payload == {
                "dispatch_id": "disp-x",
                "channel": "C1",
                "thread_ts": "1.0",
                "agent": "sam",
            }
            assert task.is_system_task
        finally:
            store.close()


class TestMarkHaltedForAgent:
    def test_writes_halt_marker_for_owned_dispatches(self, root):
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam")
        _seed_dispatch(root, dispatch_id="disp-b", agent="lisa")

        halted = supervision.mark_halted_for_agent("sam", root=root)

        assert halted == ["disp-a"]
        assert dstate.read_field("disp-a", dstate.FIELD_HALT_MARKER, root=root) is not None
        assert dstate.read_field("disp-b", dstate.FIELD_HALT_MARKER, root=root) is None

    def test_skips_already_terminal(self, root):
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam")
        dstate.write_field("disp-a", dstate.FIELD_EXITCODE, "0", root=root)

        halted = supervision.mark_halted_for_agent("sam", root=root)
        assert halted == []

    def test_skips_already_halted(self, root):
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam")
        dstate.write_field("disp-a", dstate.FIELD_HALT_MARKER, "already", root=root)

        halted = supervision.mark_halted_for_agent("sam", root=root)
        assert halted == []
