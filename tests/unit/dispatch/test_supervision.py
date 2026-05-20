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
from pathlib import Path
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
    heartbeat: bool = True,
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
    if heartbeat:
        hb = Path(root) / dispatch_id / dstate.FIELD_HEARTBEAT
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.touch()


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
        # No PR URL — only the terminal summary is posted (no auto-review).
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
        assert "<@sam>" in text

    async def test_exitcode_zero_with_pr_url_posts_summary_and_auto_review(self, root, slack_client):
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
        assert slack_client.chat_postMessage.await_count == 2
        calls = slack_client.chat_postMessage.call_args_list
        summary_text = calls[0].kwargs["text"]
        assert ":white_check_mark:" in summary_text
        assert "/pull/9" in summary_text
        review_text = calls[1].kwargs["text"]
        assert "PR ready for review" in review_text
        assert "https://github.com/o/r/pull/9" in review_text
        assert "<@sam>" in review_text

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


def _seed_slot(root: str, dispatch_id: str, slot_idx: int = 0) -> Path:
    """Write a slot file holding dispatch_id and return its path."""
    slots_dir = Path(root) / ".slots"
    slots_dir.mkdir(exist_ok=True)
    slot_path = slots_dir / f"slot-{slot_idx}"
    slot_path.write_text(dispatch_id)
    return slot_path


@pytest.mark.asyncio
class TestHalt:
    async def test_halt_marker_waits_for_exitcode_then_posts_killed(self, root, slack_client, monkeypatch):
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "killed"}
        supervision._wait_for_exitcode.assert_awaited_once()
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":octagonal_sign:" in text
        assert "killed" in text

    async def test_halt_releases_slot(self, root, slack_client, monkeypatch):
        """stuck_guard_kill path must release the dispatch's slot file."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)
        slot_file = _seed_slot(root, "disp-1", slot_idx=0)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert not slot_file.exists(), "slot file must be removed after halt"

    async def test_halt_slot_release_idempotent_when_no_slot(self, root, slack_client, monkeypatch):
        """halt path must not error when the slot was already released."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)
        # Create slots dir but no slot file (janitor already cleaned it).
        (Path(root) / ".slots").mkdir(exist_ok=True)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "killed"}


@pytest.mark.asyncio
class TestTimeout:
    async def test_exceeded_budget_writes_timeout_marker_and_waits(self, root, slack_client, monkeypatch):
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

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
        supervision._wait_for_exitcode.assert_awaited_once()
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"
        assert dstate.read_field("disp-1", dstate.FIELD_TIMEOUT_MARKER, root=root) is not None
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":alarm_clock:" in text
        assert "timed out" in text

    async def test_timeout_releases_slot(self, root, slack_client, monkeypatch):
        """runtime_timeout path must release the dispatch's slot file."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)
        slot_file = _seed_slot(root, "disp-1", slot_idx=1)

        now = started + timedelta(seconds=120)
        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=now,
        )

        assert not slot_file.exists(), "slot file must be removed after timeout"

    async def test_timeout_slot_release_idempotent_when_no_slot(self, root, slack_client, monkeypatch):
        """timeout path must not error when no slot file is present."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)
        (Path(root) / ".slots").mkdir(exist_ok=True)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=120),
        )

        assert result == {"status": "done", "reason": "timeout"}

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
    async def test_absent_heartbeat_no_exitcode_synthesizes(self, root, slack_client):
        # No heartbeat file → babysit never started or has already died.
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, started_at=started, heartbeat=False)
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

    async def test_fresh_heartbeat_no_orphan_action(self, root, slack_client):
        # Fresh heartbeat → dispatch is alive; supervisor must not declare orphan.
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, started_at=started, heartbeat=True)
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()


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
            # destination is intentionally NOT set — the supervisor reads
            # everything from payload; the destination column only
            # applies to cron-driven agent tasks.
            assert task.destination is None
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

    def test_payload_includes_agent_user_id_when_provided(self, tmp_path):
        from router.scheduled_tasks.store import ScheduledTaskStore

        store = ScheduledTaskStore(str(tmp_path / "tasks.db"))
        try:
            task = supervision.register_supervision(
                store,
                dispatch_id="disp-x",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                agent_user_id="U123ABC",
                period_seconds=60,
            )
            assert task.payload["agent_user_id"] == "U123ABC"
        finally:
            store.close()

    def test_empty_dispatch_id_rejected_at_register(self, tmp_path):
        # Validating at register time prevents the malformed-payload
        # poll-forever failure mode the reviewer called out.
        from router.scheduled_tasks.store import ScheduledTaskStore

        store = ScheduledTaskStore(str(tmp_path / "tasks.db"))
        try:
            with pytest.raises(ValueError, match="dispatch_id"):
                supervision.register_supervision(
                    store,
                    dispatch_id="",
                    channel="C1",
                    thread_ts="1.0",
                    agent="sam",
                )
            with pytest.raises(ValueError, match="agent"):
                supervision.register_supervision(
                    store,
                    dispatch_id="disp-x",
                    channel="C1",
                    thread_ts="1.0",
                    agent="",
                )
        finally:
            store.close()


class TestAgentMentionUsesUserId:
    @pytest.mark.asyncio
    async def test_terminal_message_pings_bot_user_id_when_present(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)

        payload = _payload()
        payload["agent_user_id"] = "U123ABC"

        await supervision.check_dispatch(
            payload=payload,
            slack_client=slack_client,
            dispatch_root=root,
        )
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        # Real Slack ping uses the user ID, not the persona name.
        assert "<@U123ABC>" in text
        assert "<@sam>" not in text

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_name_when_user_id_missing(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)

        await supervision.check_dispatch(
            payload=_payload(),  # no agent_user_id
            slack_client=slack_client,
            dispatch_root=root,
        )
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        # Renders as visible text but won't ping the bot — caller must
        # add agent_user_id at register time to get a real ping.
        assert "<@sam>" in text


class TestHaltDoesNotOverwriteRealExitcode:
    @pytest.mark.asyncio
    async def test_halt_race_preserves_existing_exitcode(self, root, slack_client, monkeypatch):
        """If the babysit wrote exit 0 between our state read and our
        synthetic write, we must NOT overwrite the real result.
        The terminal path (step 1) fires before the halt path (step 2)
        because exitcode is already present — _wait_for_exitcode is never reached.
        """
        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)
        # Simulate the race: a real exitcode lands before the halt path
        # gets to its synthetic write.
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        # The supervisor's terminal path runs first (exitcode is checked
        # before halt_marker), so it never reaches the synthetic write.
        # The real exitcode is preserved.
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "0"

    def test_write_synthetic_exitcode_if_absent_is_no_op_when_present(self, root):
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        actual = supervision._write_synthetic_exitcode_if_absent("disp-1", dispatch_root=root)
        # Real exitcode preserved, synthetic write skipped.
        assert actual == "0"
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "0"

    def test_write_synthetic_exitcode_if_absent_writes_when_missing(self, root):
        actual = supervision._write_synthetic_exitcode_if_absent("disp-1", dispatch_root=root)
        assert actual == "-1"
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"


@pytest.mark.asyncio
class TestSupervisorKillChannel:
    """Guards against regression to cross-namespace os.killpg (#213)."""

    async def test_supervisor_halt_does_not_call_os_killpg(self, root, slack_client, monkeypatch):
        """Regression: supervisor must never call os.killpg from the router container."""
        import os as _os

        killpg_calls: list = []
        monkeypatch.setattr(_os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value="-1"))

        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert killpg_calls == [], "supervisor must not call os.killpg (cross-namespace-blind)"

    async def test_supervisor_writes_halt_marker_then_waits_for_exitcode(self, root, slack_client, monkeypatch):
        """Halt path must wait for babysit's exitcode instead of sending SIGTERM."""
        waited_for: list[str] = []

        async def fake_wait(dispatch_id, *, dispatch_root, **kwargs):
            waited_for.append(dispatch_id)
            return None  # simulate 60 s timeout: babysit unresponsive

        monkeypatch.setattr(supervision, "_wait_for_exitcode", fake_wait)

        _seed_dispatch(root, pid=12345)
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "killed"}
        assert waited_for == ["disp-1"], "_wait_for_exitcode must be called once"
        assert dstate.read_field("disp-1", dstate.FIELD_EXITCODE, root=root) == "-1"
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":octagonal_sign:" in text

    async def test_supervisor_timeout_writes_timeout_marker_then_waits(self, root, slack_client, monkeypatch):
        """Budget-exceeded path must write timeout_marker and wait for babysit."""
        waited_for: list[str] = []

        async def fake_wait(dispatch_id, *, dispatch_root, **kwargs):
            waited_for.append(dispatch_id)
            return None

        monkeypatch.setattr(supervision, "_wait_for_exitcode", fake_wait)

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=120),
        )

        assert waited_for == ["disp-1"]
        assert dstate.read_field("disp-1", dstate.FIELD_TIMEOUT_MARKER, root=root) is not None


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


@pytest.mark.asyncio
class TestAutoReview:
    """Issue #207 — auto-invoke PR review on successful dispatch completion."""

    async def test_auto_review_posts_mention_with_pr_url(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/42", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        # Two posts: terminal summary + auto-review mention.
        assert slack_client.chat_postMessage.await_count == 2
        review_text = slack_client.chat_postMessage.call_args_list[1].kwargs["text"]
        assert "PR ready for review" in review_text
        assert "https://github.com/o/r/pull/42" in review_text
        assert "<@sam>" in review_text
        assert "disp-1" in review_text

    async def test_auto_review_uses_agent_user_id_when_provided(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/42", root=root)

        payload = _payload()
        payload["agent_user_id"] = "U999XYZ"

        await supervision.check_dispatch(
            payload=payload,
            slack_client=slack_client,
            dispatch_root=root,
        )

        review_text = slack_client.chat_postMessage.call_args_list[1].kwargs["text"]
        assert "<@U999XYZ>" in review_text
        assert "<@sam>" not in review_text

    async def test_auto_review_idempotent_when_marker_exists(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/42", root=root)
        # Pre-write the idempotency marker (simulates a previous run).
        marker = Path(root) / "disp-1" / dstate.FIELD_AUTO_REVIEW_FIRED
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        # Only the terminal summary is posted; auto-review suppressed.
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":white_check_mark:" in text

    async def test_auto_review_writes_marker_file(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/42", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        marker = Path(root) / "disp-1" / dstate.FIELD_AUTO_REVIEW_FIRED
        assert marker.exists(), "auto_review_fired marker must be written"

    async def test_auto_review_not_fired_on_nonzero_exit(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "1", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/42", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        # Only the failure summary; no auto-review message.
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":x:" in text

    async def test_auto_review_not_fired_without_pr_url(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        # No FIELD_PR_URL written.

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        # Only the terminal summary; no auto-review message.
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":white_check_mark:" in text

    async def test_auto_review_posted_in_dispatch_thread(self, root, slack_client):
        _seed_dispatch(root, channel="C999", thread_ts="88.0")
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/5", root=root)

        await supervision.check_dispatch(
            payload=_payload(channel="C999", thread_ts="88.0"),
            slack_client=slack_client,
            dispatch_root=root,
        )

        review_call = slack_client.chat_postMessage.call_args_list[1]
        assert review_call.kwargs["channel"] == "C999"
        assert review_call.kwargs["thread_ts"] == "88.0"
