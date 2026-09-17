"""Named regression test for #899 — auto-invoke Sam post-mortem on timeout.

Drives ``check_dispatch`` through the real termination paths and asserts:

* a timeout/``budget_overrun`` terminal enqueues exactly one post-mortem
  request + writes the ``FIELD_POST_MORTEM_FIRED`` idempotency marker;
* a hard-fail (exitcode) terminal enqueues zero (#867's path is untouched);
* an orphan terminal also enqueues zero;
* a second tick against an already-fired timeout is a no-op (no duplicate
  queue entry, no duplicate marker write).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import post_mortem, supervision
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
    issue_url: str = "https://github.com/o/r/issues/900",
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
    dstate.write_field(dispatch_id, dstate.FIELD_ISSUE_URL, issue_url, root=root)
    if heartbeat:
        hb = dstate.dispatch_dir(dispatch_id, root=root) / dstate.FIELD_HEARTBEAT
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
class TestPostMortemEnqueueOnTimeout:
    async def test_budget_overrun_enqueues_exactly_one_post_mortem(self, root, slack_client, monkeypatch):
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=120),
        )

        assert result == {"status": "done", "reason": "timeout"}

        marker = dstate.dispatch_dir("disp-1", root=root) / dstate.FIELD_POST_MORTEM_FIRED
        assert marker.exists()

        queued = post_mortem.read_queue(root)
        assert len(queued) == 1
        request = queued[0]
        assert request["dispatch_id"] == "disp-1"
        assert request["issue_url"] == "https://github.com/o/r/issues/900"
        assert request["reason"] == supervision.HaltReason.BUDGET_OVERRUN
        assert request["elapsed_seconds"] == 120
        assert request["budget_seconds"] == 60
        assert request["channel"] == "C123"
        assert request["thread_ts"] == "1.0"

    async def test_second_tick_on_same_dispatch_does_not_double_enqueue(self, root, slack_client, monkeypatch):
        """A router restart replaying the same timeout tick must not double-fire."""
        monkeypatch.setattr(supervision, "_wait_for_exitcode", AsyncMock(return_value=None))
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, pid=5555, started_at=started, budget=60)

        now = started + timedelta(seconds=120)
        await supervision.check_dispatch(payload=_payload(), slack_client=slack_client, dispatch_root=root, now=now)
        assert len(post_mortem.read_queue(root)) == 1

        # Directly re-exercise the idempotent enqueue seam a second time —
        # mirrors a re-fired tick against the same already-terminal dispatch.
        enqueued_again = post_mortem.enqueue(
            "disp-1",
            issue_url="https://github.com/o/r/issues/900",
            workspace_path=str(dstate.dispatch_dir("disp-1", root=root)),
            reason=supervision.HaltReason.BUDGET_OVERRUN,
            elapsed_seconds=120,
            budget_seconds=60,
            channel="C123",
            thread_ts="1.0",
            dispatch_root=root,
        )

        assert enqueued_again is False
        assert len(post_mortem.read_queue(root)) == 1


@pytest.mark.asyncio
class TestPostMortemNotEnqueuedOnOtherTerminals:
    async def test_hard_fail_exitcode_enqueues_zero(self, root, slack_client):
        """#867's hard-fail path is untouched — no post-mortem for a real code failure."""
        _seed_dispatch(root, pid=os.getpid())
        dstate.write_field("disp-1", dstate.FIELD_EXITCODE, "1", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 1}
        assert post_mortem.read_queue(root) == []
        marker = dstate.dispatch_dir("disp-1", root=root) / dstate.FIELD_POST_MORTEM_FIRED
        assert not marker.exists()

    async def test_orphan_enqueues_zero(self, root, slack_client):
        started = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
        _seed_dispatch(root, started_at=started, heartbeat=False)

        result = await supervision.check_dispatch(
            payload=_payload(),
            slack_client=slack_client,
            dispatch_root=root,
            now=started + timedelta(seconds=30),
        )

        assert result == {"status": "done", "reason": "orphan"}
        assert post_mortem.read_queue(root) == []
        marker = dstate.dispatch_dir("disp-1", root=root) / dstate.FIELD_POST_MORTEM_FIRED
        assert not marker.exists()
