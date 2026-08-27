"""Unit tests for router.dispatch.supervision — the polling check_dispatch.

Covers every detection path from the issue's AC matrix: terminal,
halted, timeout, orphan, delta, quiet. Also exercises the
``mark_halted_for_agent`` helper used by ``/kill``.

State files are written into a tmp_path fixture and supervision is
invoked with ``dispatch_root=tmp_path``; the real ``/var/lib/dispatch``
volume is never touched.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import feed_transport, supervision
from router.dispatch import state as dstate

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
        # Issue #270: the terminal summary speaks as the workers bot and carries
        # no agent @-mention (it would otherwise wake the agent on its own
        # done-line). The deliberate wake is the separate auto-review handoff.
        assert "<@" not in text

    async def test_exitcode_zero_with_pr_url_posts_single_merged_completion(self, root, slack_client):
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
        # Issue #333: exactly one merged completion line instead of two.
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":white_check_mark:" in text
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

    async def test_orphan_releases_slot(self, root, slack_client):
        """orphan path must release the dispatch's slot file."""
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, started_at=started, heartbeat=False)
        slot_file = _seed_slot(root, "disp-1", slot_idx=2)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert not slot_file.exists(), "slot file must be removed after orphan"

    async def test_orphan_slot_release_idempotent_when_no_slot(self, root, slack_client):
        """orphan path must not error when no slot file is present."""
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, started_at=started, heartbeat=False)
        (Path(root) / ".slots").mkdir(exist_ok=True)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "done", "reason": "orphan"}

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

    def test_payload_includes_transport_ref_when_provided(self, tmp_path):
        """#713: transport/conversation_id flow into the payload so
        check_dispatch can resolve a ChatAdapter for the dispatch."""
        from router.scheduled_tasks.store import ScheduledTaskStore

        store = ScheduledTaskStore(str(tmp_path / "tasks.db"))
        try:
            task = supervision.register_supervision(
                store,
                dispatch_id="disp-x",
                channel="",
                thread_ts="",
                agent="sam",
                transport="discord",
                conversation_id="discord:1:2:3",
                period_seconds=60,
            )
            assert task.payload["transport"] == "discord"
            assert task.payload["conversation_id"] == "discord:1:2:3"
        finally:
            store.close()

    def test_payload_omits_transport_ref_when_absent(self, tmp_path):
        """Slack path (and every pre-#713 caller): no transport/conversation_id
        kwargs → no spurious keys in the payload (byte-compat regression)."""
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
            assert "transport" not in task.payload
            assert "conversation_id" not in task.payload
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


class TestTerminalDropsAgentMention:
    """Issue #270: the terminal summary no longer @-mentions the agent.

    The post now speaks as the workers bot — an allowlisted dispatch-bot
    sender — so a mention would wake the agent on its own done-line instead of
    being self-dropped. The mention that *does* survive is the auto-review
    handoff (see :class:`TestAutoReview`), which exists to wake the agent.
    """

    @pytest.mark.asyncio
    async def test_terminal_drops_mention_even_with_user_id(self, root, slack_client):
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
        # No mention of any kind — neither the resolved user id nor the persona.
        assert "<@U123ABC>" not in text
        assert "<@sam>" not in text
        assert "<@" not in text

    @pytest.mark.asyncio
    async def test_terminal_drops_mention_without_user_id(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)

        await supervision.check_dispatch(
            payload=_payload(),  # no agent_user_id
            slack_client=slack_client,
            dispatch_root=root,
        )
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "<@sam>" not in text
        assert "<@" not in text


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

    async def test_supervisor_timeout_writes_cancel_reason_before_marker(self, root, slack_client, monkeypatch):
        """Cause-before-marker (issue #385): cancel_reason must be written
        before timeout_marker so a babysit racing to detect the marker can
        never write its own exitcode ahead of the cause landing on disk."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        write_order: list[str] = []
        real_write_field = dstate.write_field

        def _tracking_write_field(dispatch_id, field, value, **kwargs):
            write_order.append(field)
            return real_write_field(dispatch_id, field, value, **kwargs)

        monkeypatch.setattr(dstate, "write_field", _tracking_write_field)

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=120),
        )

        cancel_idx = write_order.index(dstate.FIELD_CANCEL_REASON)
        marker_idx = write_order.index(dstate.FIELD_TIMEOUT_MARKER)
        assert cancel_idx < marker_idx, f"cancel_reason must precede timeout_marker, got order {write_order}"
        assert dstate.read_field("disp-1", dstate.FIELD_CANCEL_REASON, root=root) == "runtime_timeout"


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

    def test_thread_scope_spares_sibling_dispatch(self, root):
        # Regression for #256: a thread-scoped halt must only touch the
        # dispatch in that thread, not a sibling the agent runs elsewhere.
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam", channel="C1", thread_ts="1.0")
        _seed_dispatch(root, dispatch_id="disp-b", agent="sam", channel="C2", thread_ts="2.0")

        halted = supervision.mark_halted_for_agent("sam", channel="C1", thread_ts="1.0", root=root)

        assert halted == ["disp-a"]
        assert dstate.read_field("disp-a", dstate.FIELD_HALT_MARKER, root=root) is not None
        assert dstate.read_field("disp-b", dstate.FIELD_HALT_MARKER, root=root) is None

    def test_thread_scope_records_scope_in_halt_reason(self, root):
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam", channel="C1", thread_ts="1.0")

        supervision.mark_halted_for_agent("sam", channel="C1", thread_ts="1.0", root=root)

        payload = _read_halt_reason(root, "disp-a")
        assert payload["metrics"]["scope"] == "thread"

    def test_broadcast_halts_all_owned_dispatches(self, root):
        # No channel/thread → the explicit ``/kill <agent> all`` broadcast.
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam", channel="C1", thread_ts="1.0")
        _seed_dispatch(root, dispatch_id="disp-b", agent="sam", channel="C2", thread_ts="2.0")

        halted = supervision.mark_halted_for_agent("sam", root=root)

        assert halted == ["disp-a", "disp-b"]
        assert dstate.read_field("disp-a", dstate.FIELD_HALT_MARKER, root=root) is not None
        assert dstate.read_field("disp-b", dstate.FIELD_HALT_MARKER, root=root) is not None
        assert _read_halt_reason(root, "disp-a")["metrics"]["scope"] == "broadcast"


def _read_halt_reason(root: str, dispatch_id: str) -> dict:
    """Parse the halt_reason JSON sidecar for a dispatch."""
    raw = dstate.read_field(dispatch_id, dstate.FIELD_HALT_REASON, root=root)
    assert raw is not None, "halt_reason file must exist on disk"
    return json.loads(raw)


def _assert_halt_reason_schema(payload: dict, *, reason: str) -> None:
    """Every halt_reason record must carry the #255 forensic schema."""
    assert payload["reason"] == reason
    assert isinstance(payload["detail"], str) and payload["detail"]
    # ``at`` must be a parseable ISO-8601 timestamp.
    datetime.fromisoformat(payload["at"])
    assert isinstance(payload["supervisor_pid"], int)
    assert isinstance(payload["metrics"], dict)


class TestHaltReason:
    """Issue #255 — every supervisor kill path leaves a halt_reason forensic
    file *and* emits a single ``supervisor halting dispatch`` WARNING line."""

    def test_manual_cancel_via_mark_halted(self, root, caplog):
        _seed_dispatch(root, dispatch_id="disp-a", agent="sam")

        with caplog.at_level(logging.WARNING, logger="router.dispatch.supervision"):
            supervision.mark_halted_for_agent("sam", root=root)

        # Both files land next to each other.
        assert dstate.read_field("disp-a", dstate.FIELD_HALT_MARKER, root=root) is not None
        payload = _read_halt_reason(root, "disp-a")
        _assert_halt_reason_schema(payload, reason=supervision.HaltReason.MANUAL_CANCEL)
        # #259: agent name lives in ``detail`` only — not in ``metrics``,
        # which is reserved for numeric / cardinality signals.
        assert "sam" in payload["detail"]
        assert "agent" not in payload["metrics"]
        # WARNING log line emitted with the canonical prefix + dispatch id.
        assert any("supervisor halting dispatch" in r.message and "disp-a" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_manual_cancel_via_check_dispatch_when_reason_absent(self, root, slack_client, monkeypatch, caplog):
        """A halt_marker that arrived without a halt_reason still leaves a trail."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))
        _seed_dispatch(root, pid=12345)
        # Marker present but no sibling reason (simulates a forgetful writer).
        dstate.write_field("disp-1", dstate.FIELD_HALT_MARKER, "now", root=root)

        with caplog.at_level(logging.WARNING, logger="router.dispatch.supervision"):
            result = await supervision.check_dispatch(payload=_payload(), slack_client=slack_client, dispatch_root=root)

        assert result == {"status": "done", "reason": "killed"}
        payload = _read_halt_reason(root, "disp-1")
        _assert_halt_reason_schema(payload, reason=supervision.HaltReason.MANUAL_CANCEL)
        assert any("supervisor halting dispatch" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_check_dispatch_does_not_clobber_existing_reason(self, root, slack_client, monkeypatch):
        """mark_halted's richer record wins — the reactor tick must not overwrite it."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))
        _seed_dispatch(root, pid=12345)
        # mark_halted_for_agent writes both the marker and the reason.
        supervision.mark_halted_for_agent("sam", root=root)
        original = _read_halt_reason(root, "disp-1")

        await supervision.check_dispatch(payload=_payload(), slack_client=slack_client, dispatch_root=root)

        assert _read_halt_reason(root, "disp-1") == original

    @pytest.mark.asyncio
    async def test_budget_overrun(self, root, slack_client, monkeypatch, caplog):
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)

        with caplog.at_level(logging.WARNING, logger="router.dispatch.supervision"):
            result = await supervision.check_dispatch(
                payload=_payload(),
                slack_client=slack_client,
                dispatch_root=root,
                now=started + timedelta(seconds=120),
            )

        assert result == {"status": "done", "reason": "timeout"}
        payload = _read_halt_reason(root, "disp-1")
        _assert_halt_reason_schema(payload, reason=supervision.HaltReason.BUDGET_OVERRUN)
        assert payload["metrics"] == {"elapsed_seconds": 120, "budget_seconds": 60}
        assert any("supervisor halting dispatch" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_heartbeat_missing(self, root, slack_client, caplog):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        # No heartbeat file → orphan path fires.
        _seed_dispatch(root, started_at=started, heartbeat=False)

        with caplog.at_level(logging.WARNING, logger="router.dispatch.supervision"):
            result = await supervision.check_dispatch(
                payload=_payload(),
                slack_client=slack_client,
                dispatch_root=root,
                now=started + timedelta(seconds=30),
            )

        assert result == {"status": "done", "reason": "orphan"}
        payload = _read_halt_reason(root, "disp-1")
        _assert_halt_reason_schema(payload, reason=supervision.HaltReason.HEARTBEAT_MISSING)
        assert payload["metrics"]["threshold_seconds"] == dstate.HEARTBEAT_STALE_SECONDS
        assert any("supervisor halting dispatch" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_heartbeat_orphan_driven_by_injected_clock(self, root, slack_client):
        """#259: the orphan path reads the injected clock, not ``time.time()``.

        A heartbeat file whose mtime is fresh against the real wall clock
        still orphans once the injected ``now`` advances past the staleness
        threshold, and ``hb_age`` is computed from that same clock — so a
        fake clock fully controls this path. Under the old code this case
        would read as alive (heartbeat_alive) and mis-measure hb_age.
        """
        real_now = datetime.now(timezone.utc)
        _seed_dispatch(root, started_at=real_now, heartbeat=True)
        # Pin the heartbeat mtime to a known, real-clock-fresh instant.
        hb_path = Path(root) / "disp-1" / dstate.FIELD_HEARTBEAT
        os.utime(hb_path, (real_now.timestamp(), real_now.timestamp()))
        now = real_now + timedelta(seconds=dstate.HEARTBEAT_STALE_SECONDS + 100)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=now,
        )

        assert result == {"status": "done", "reason": "orphan"}
        payload = _read_halt_reason(root, "disp-1")
        assert payload["metrics"]["heartbeat_age_seconds"] == dstate.HEARTBEAT_STALE_SECONDS + 100

    def test_write_halt_reason_survives_disk_failure(self, root, monkeypatch, caplog):
        """A write failure is logged but the WARNING still fires (pure instrumentation)."""
        monkeypatch.setattr(
            supervision.dstate,
            "write_field",
            MagicMock(side_effect=OSError("volume full")),
        )
        with caplog.at_level(logging.WARNING, logger="router.dispatch.supervision"):
            payload = supervision._write_halt_reason(
                "disp-z",
                reason=supervision.HaltReason.BUDGET_OVERRUN,
                detail="simulated",
                now=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
                dispatch_root=root,
            )

        assert payload["reason"] == "budget_overrun"
        assert any("supervisor halting dispatch" in r.message for r in caplog.records)


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

        # Issue #333: single merged completion line (no longer two posts).
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "https://github.com/o/r/pull/42" in text
        assert "<@sam>" in text
        assert "disp-1" in text

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

        # Issue #333: merged into one post; user_id mention in that post.
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "<@U999XYZ>" in text
        assert "<@sam>" not in text

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

        # Issue #333: marker present means the merged completion line was already
        # posted in a prior run — skip entirely (0 posts, not 1).
        slack_client.chat_postMessage.assert_not_awaited()

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

        # Issue #333: merged into one post; that post must land in the dispatch thread.
        slack_client.chat_postMessage.assert_awaited_once()
        call = slack_client.chat_postMessage.call_args
        assert call.kwargs["channel"] == "C999"
        assert call.kwargs["thread_ts"] == "88.0"


@pytest.mark.asyncio
class TestPostsThroughInjectedClient:
    """Issue #270: supervision is identity-agnostic — it posts through whatever
    Slack client it is handed and never constructs one of its own.

    The workers-bot identity for dispatch-supervision posts is chosen upstream,
    by the scheduler's ``system_client_resolver`` (see
    ``tests/unit/scheduled_tasks/test_scheduler.py``). Keeping the client a
    pure injection point is what lets the supervision smoke test drive the full
    pipeline with the Slack client as its only seam — a regression that has
    supervision build its own client (e.g. off ``WORKERS_BOT_TOKEN``) would
    bypass that seam and make a real API call, exactly the #273 failure.
    """

    async def test_terminal_uses_injected_client_regardless_of_workers_token(self, root, slack_client, monkeypatch):
        # Even with the token present, supervision must not build its own client.
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-present")

        _seed_dispatch(root, pid=os.getpid())
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/7", root=root)

        payload = _payload()
        payload["agent_user_id"] = "U777"

        await supervision.check_dispatch(
            payload=payload,
            slack_client=slack_client,
            dispatch_root=root,
        )

        # Issue #333: one merged completion line (not two). Still through
        # the injected client; supervision constructed nothing of its own.
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "<@U777>" in text


class TestFormatRateLimitEvent:
    """Unit tests for the pure _format_rate_limit_event formatter (#301).

    Covers the four cases the issue mandates: allowed payload, blocked payload,
    malformed payload (missing rate_limit_info / unexpected shape), and an
    unknown status value.  No Slack client is involved — the function is pure.
    """

    _NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)

    def test_allowed_payload_formats_details(self):
        resets_at = int(self._NOW.timestamp()) + 3900  # 1h05m from now
        info = json.dumps(
            {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "resetsAt": resets_at,
                "overageStatus": "allowed",
                "isUsingOverage": False,
            }
        )
        result = supervision._format_rate_limit_event(info, self._NOW)
        assert "event: rate_limit_event" in result
        assert "status: allowed" in result
        assert "type: five_hour" in result
        assert "resets in ~1h05m" in result
        assert "isUsingOverage" not in result
        assert "⚠️" not in result

    def test_blocked_payload_prefixes_warning(self):
        resets_at = int(self._NOW.timestamp()) + 300  # 5m from now
        info = json.dumps(
            {
                "status": "blocked",
                "rateLimitType": "weekly",
                "resetsAt": resets_at,
                "isUsingOverage": True,
            }
        )
        result = supervision._format_rate_limit_event(info, self._NOW)
        assert result.startswith("⚠️")
        assert "event: rate_limit_event" in result
        assert "status: blocked" in result
        assert "type: weekly" in result
        assert "resets in ~5m" in result
        assert "isUsingOverage: true" in result

    def test_missing_rate_limit_info_falls_back(self):
        result = supervision._format_rate_limit_event(None, self._NOW)
        assert result == "event: rate_limit_event"

    def test_malformed_json_falls_back(self):
        result = supervision._format_rate_limit_event("not-json{{{", self._NOW)
        assert result == "event: rate_limit_event"

    def test_non_dict_json_falls_back(self):
        result = supervision._format_rate_limit_event(json.dumps([1, 2, 3]), self._NOW)
        assert result == "event: rate_limit_event"

    def test_unknown_status_prefixes_warning(self):
        info = json.dumps({"status": "queueing", "rateLimitType": "five_hour"})
        result = supervision._format_rate_limit_event(info, self._NOW)
        assert result.startswith("⚠️")
        assert "status: queueing" in result

    def test_resets_at_past_shows_raw_epoch(self):
        resets_at = int(self._NOW.timestamp()) - 60  # already in the past
        info = json.dumps({"status": "allowed", "rateLimitType": "five_hour", "resetsAt": resets_at})
        result = supervision._format_rate_limit_event(info, self._NOW)
        assert f"resetsAt: {resets_at}" in result
        assert "resets in" not in result

    def test_is_using_overage_omitted_when_false(self):
        info = json.dumps({"status": "allowed", "rateLimitType": "five_hour", "isUsingOverage": False})
        result = supervision._format_rate_limit_event(info, self._NOW)
        assert "isUsingOverage" not in result


@pytest.mark.asyncio
class TestRateLimitEventDelta:
    """End-to-end: rate_limit_event in the delta path posts enriched Slack text (#301)."""

    async def test_allowed_rate_limit_event_posts_details(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        resets_at = int(started.timestamp()) + 3900
        rate_limit_info = json.dumps(
            {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "resetsAt": resets_at,
                "isUsingOverage": False,
            }
        )
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "rate_limit_event", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_RATE_LIMIT_INFO, rate_limit_info, root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "rate_limit_event" in text
        assert "status: allowed" in text
        assert "type: five_hour" in text
        assert "resets in ~" in text
        assert "⚠️" not in text

    async def test_blocked_rate_limit_event_shows_warning(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        resets_at = int(started.timestamp()) + 300
        rate_limit_info = json.dumps(
            {
                "status": "blocked",
                "rateLimitType": "five_hour",
                "resetsAt": resets_at,
                "isUsingOverage": True,
            }
        )
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "rate_limit_event", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_RATE_LIMIT_INFO, rate_limit_info, root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "⚠️" in text
        assert "status: blocked" in text
        assert "isUsingOverage: true" in text

    async def test_rate_limit_event_without_info_falls_back(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        # Write the event type but no rate_limit_info sidecar.
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "rate_limit_event", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "event: rate_limit_event" in text
        assert "⚠️" not in text

    async def test_non_rate_limit_events_unchanged(self, root, slack_client):
        """AC #5: non-rate_limit_event posts must not change format."""
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "event: assistant" in text
        assert "rate_limit_event" not in text
        assert "⚠️" not in text


# ── Issue #333 scoped tests ──────────────────────────────────────────────────


class TestDoublePrefix:
    """AC: No dispatch-dispatch- double prefix in any Slack lifecycle line."""

    def test_extract_issue_num_from_github_url(self):
        url = "https://github.com/owner/repo/issues/318"
        assert supervision._extract_issue_num(url) == "#318"

    def test_extract_issue_num_absent_when_no_url(self):
        assert supervision._extract_issue_num(None) == ""
        assert supervision._extract_issue_num("") == ""

    def test_extract_issue_num_absent_when_url_not_issue(self):
        assert supervision._extract_issue_num("https://github.com/owner/repo") == ""

    def test_id_label_issue_num_and_summary(self):
        label = supervision._id_label("disp-1", "https://github.com/o/r/issues/42", "My summary")
        assert label == '#42 "My summary"'

    def test_id_label_issue_num_only(self):
        label = supervision._id_label("disp-1", "https://github.com/o/r/issues/42", None)
        assert label == "#42"

    def test_id_label_fallback_to_dispatch_id(self):
        label = supervision._id_label("disp-1", None, None)
        assert label == "`disp-1`"


@pytest.mark.asyncio
class TestCostRounding:
    """AC: Cost rendered rounded to 2 decimal places."""

    async def test_cost_rounded_in_terminal_summary(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_COST, "0.7869120000000004", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "$0.79" in text
        assert "0.7869" not in text

    async def test_cost_rounded_in_merged_completion(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_COST, "0.7869120000000004", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "$0.79" in text
        assert "0.7869" not in text


@pytest.mark.asyncio
class TestEventUserSuppression:
    """AC: event: user progress line is never emitted to Slack."""

    async def test_event_user_suppressed(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "user", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_other_events_still_posted(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "event: assistant" in text


@pytest.mark.asyncio
class TestMergedCompletionLine:
    """AC: exactly one completion line with @Sam mention and PR link."""

    async def test_single_line_with_mention_and_pr(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/1", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":white_check_mark:" in text
        assert "https://github.com/o/r/pull/1" in text
        assert "<@sam>" in text

    async def test_no_mention_when_no_pr(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "<@" not in text

    async def test_no_mention_on_failure(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "1", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/1", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "<@" not in text


@pytest.mark.asyncio
class TestSummaryRendering:
    """AC: summary field present/absent rendering in completion line."""

    async def test_summary_present_in_completion_line(self, root, slack_client):
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)
        dstate.write_field("disp-1", dstate.FIELD_ISSUE_URL, "https://github.com/o/r/issues/42", root=root)
        dstate.write_field("disp-1", dstate.FIELD_SUMMARY, "Fix the thing", root=root)

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "#42" in text
        assert "Fix the thing" in text

    async def test_summary_absent_no_artifact(self, root, slack_client):
        """When summary is absent, lines degrade gracefully — no None/empty-string artifact."""
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)
        dstate.write_field("disp-1", dstate.FIELD_ISSUE_URL, "https://github.com/o/r/issues/42", root=root)
        # No FIELD_SUMMARY written.

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "#42" in text
        assert "None" not in text
        assert '""' not in text

    async def test_no_issue_url_fallback_to_dispatch_id(self, root, slack_client):
        """No issue_url → dispatch_id used as fallback, no crash."""
        _seed_dispatch(root)
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)
        # No FIELD_ISSUE_URL or FIELD_SUMMARY.

        await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "disp-1" in text
        assert "None" not in text


@pytest.mark.asyncio
class TestQueueContention:
    """Issue #496: budget clock must start at slot acquisition, not queue entry."""

    async def test_dispatch_not_killed_on_first_tick_after_long_queue_wait(self, root, slack_client, monkeypatch):
        """A dispatch that waited in queue longer than its budget should NOT be
        budget-killed on the first supervision tick after acquiring a slot."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        budget = 1800  # 30 min budget
        queue_entry = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        # Simulate 29 minutes spent in the FIFO queue before slot acquisition.
        run_start = queue_entry + timedelta(seconds=1740)

        _seed_dispatch(root, pid=os.getpid(), started_at=queue_entry, budget=budget)
        dstate.write_field("disp-1", dstate.FIELD_RUN_STARTED_AT, run_start.isoformat(), root=root)

        # First supervision tick fires 5 seconds after the slot was acquired.
        now = run_start + timedelta(seconds=5)
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=now,
        )

        # Must keep polling — NOT fired as timeout.
        assert result == {"status": "ok"}, (
            "Dispatch was incorrectly budget-killed; queue wait time must not count against runtime budget"
        )
        slack_client.chat_postMessage.assert_not_awaited()
        assert dstate.read_field("disp-1", dstate.FIELD_TIMEOUT_MARKER, root=root) is None

    async def test_dispatch_killed_after_full_budget_of_actual_runtime(self, root, slack_client, monkeypatch):
        """After the full budget elapses since run_started_at, the supervisor fires normally."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        budget = 1800  # 30 min
        queue_entry = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        run_start = queue_entry + timedelta(seconds=1740)

        _seed_dispatch(root, pid=5555, started_at=queue_entry, budget=budget)
        dstate.write_field("disp-1", dstate.FIELD_RUN_STARTED_AT, run_start.isoformat(), root=root)

        # Tick fires 31 minutes after slot acquisition — budget exhausted.
        now = run_start + timedelta(seconds=1860)
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=now,
        )

        assert result == {"status": "done", "reason": "timeout"}
        assert dstate.read_field("disp-1", dstate.FIELD_TIMEOUT_MARKER, root=root) is not None

    async def test_fallback_to_started_at_when_run_started_at_absent(self, root, slack_client, monkeypatch):
        """Pre-#496 dispatches with no run_started_at still trigger budget kill via started_at."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)
        # No run_started_at written — simulates a dispatch created before the fix.

        now = started + timedelta(seconds=120)
        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=now,
        )

        assert result == {"status": "done", "reason": "timeout"}


class TestSlotReleaseImportLogging:
    """AC: WARNING log emitted when slot-release dynamic import fails (issue #423)."""

    def test_warning_emitted_on_import_failure(self, caplog):
        import importlib as _importlib
        from unittest.mock import patch

        try:
            with caplog.at_level(logging.WARNING, logger="router.dispatch.supervision"):
                with patch(
                    "importlib.util.spec_from_file_location",
                    side_effect=RuntimeError("simulated import failure"),
                ):
                    _importlib.reload(supervision)

            records = [
                r
                for r in caplog.records
                if r.levelno >= logging.WARNING and "slot-release import failed" in r.getMessage()
            ]
            assert records, "Expected a WARNING log record for slot-release import failure"
            assert records[0].exc_info is not None, "Expected traceback (exc_info) in the log record"
        finally:
            _importlib.reload(supervision)


def _discord_payload(
    *,
    dispatch_id: str = "disp-1",
    agent: str = "sam",
    conversation_id: str = "discord:1:2:3",
) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "channel": "",
        "thread_ts": "",
        "agent": agent,
        "transport": "discord",
        "conversation_id": conversation_id,
    }


@pytest.mark.asyncio
class TestChatAdapterRouting:
    """#713: milestone_feed/supervision posts route through a ChatAdapter
    resolved from the dispatch's transport/conversation_id, behind the
    default-off DISPATCH_FEED_VIA_CHAT_ADAPTER flag."""

    def _discord_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.send_message = AsyncMock()
        return adapter

    async def test_flag_off_discord_ref_still_uses_slack_path(self, root, slack_client, monkeypatch):
        """Flag off: even with a persisted Discord ref, behaviour is
        byte-for-byte the pre-#713 log-and-skip (channel/thread_ts empty)."""
        monkeypatch.delenv(feed_transport.ENV_FLAG, raising=False)
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started, channel="", thread_ts="")
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_TOOL, "Edit", root=root)

        result = await supervision.check_dispatch(
            payload=_discord_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_flag_on_slack_payload_unchanged(self, root, slack_client, monkeypatch):
        """Flag on but a Slack (no-transport) payload — identical to flag off."""
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started)
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_TOOL, "Edit", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_awaited_once()

    async def test_flag_on_discord_delta_posts_via_adapter(self, root, slack_client, monkeypatch):
        """Flag on + resolvable Discord adapter: the delta line lands via the
        adapter, never via the (channel-less) Slack client."""
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        adapter = self._discord_adapter()
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started, channel="", thread_ts="")
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_TOOL, "Edit", root=root)

        result = await supervision.check_dispatch(
            payload=_discord_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()
        adapter.send_message.assert_awaited_once()
        outbound = adapter.send_message.await_args.args[0]
        assert str(outbound.conversation_ref) == "discord:1:2:3"
        assert "tool: Edit" in outbound.text

    async def test_flag_on_terminal_summary_posts_via_adapter(self, root, slack_client, monkeypatch):
        """Flag on: the terminal (no-PR) summary line also routes via the adapter."""
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        adapter = self._discord_adapter()
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        _seed_dispatch(root, pid=os.getpid(), channel="", thread_ts="")
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "0", root=root)

        result = await supervision.check_dispatch(
            payload=_discord_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 0}
        slack_client.chat_postMessage.assert_not_awaited()
        adapter.send_message.assert_awaited_once()

    async def test_flag_on_missing_conversation_id_degrades_to_log_and_skip(self, root, slack_client, monkeypatch):
        """Flag on + transport=discord but no conversation_id (ref not yet
        threaded through, e.g. a dispatch launched before #713) — degrades
        to the historical log-and-skip, no crash, no adapter lookup posts."""
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started, channel="", thread_ts="")
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_TOOL, "Edit", root=root)

        result = await supervision.check_dispatch(
            payload=_discord_payload(conversation_id=""),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_flag_on_unknown_agent_adapter_degrades_to_log_and_skip(self, root, slack_client, monkeypatch):
        """Flag on + a Discord ref but no adapter running for this agent —
        clear degrade, no crash, no silent Slack fallback."""
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: None)

        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=os.getpid(), started_at=started, channel="", thread_ts="")
        dstate.write_field("disp-1", dstate.FIELD_LAST_EVENT, "assistant", root=root)
        dstate.write_field("disp-1", dstate.FIELD_LAST_TOOL, "Edit", root=root)

        result = await supervision.check_dispatch(
            payload=_discord_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "ok"}
        slack_client.chat_postMessage.assert_not_awaited()
