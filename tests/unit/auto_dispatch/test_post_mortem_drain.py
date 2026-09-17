"""Named regression test for #899 — the auto_dispatch drain leg.

Covers the two remaining legs of the contract not exercised by
``tests/unit/dispatch/test_supervision_post_mortem.py`` (which drives the
enqueue side through the real ``check_dispatch`` termination path):

* the drain leg builds the correct Sam post-mortem dispatch cmd;
* the drain leg respects a full #866 concurrency cap (request stays
  queued, not dropped, not retried in the same tick);
* a tripped #868 breaker suppresses the drain the same way.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from router.auto_dispatch.circuit_breaker import _breaker_path, trip
from router.auto_dispatch.loop import _drain_post_mortem_queue
from router.dispatch import post_mortem

pytestmark = pytest.mark.unit


def _payload(tmp_path) -> dict:
    return {
        "dispatch_root": str(tmp_path),
        "counter_path": str(tmp_path / "_auto_dispatch_counters.json"),
    }


def _request(**overrides) -> dict:
    base = {
        "dispatch_id": "disp-dead-1",
        "issue_url": "https://github.com/o/r/issues/900",
        "workspace_path": "/var/lib/dispatch/disp-dead-1",
        "reason": "budget_overrun",
        "elapsed_seconds": 1810,
        "budget_seconds": 1800,
        "channel": "C123",
        "thread_ts": "1.0",
        "transport": "",
        "conversation_id": "",
    }
    base.update(overrides)
    return base


class TestBuildDispatchCmd:
    def test_cmd_shape_targets_sam_with_short_budget_and_exec_override(self):
        request = _request()
        cmd = post_mortem.build_dispatch_cmd(request)

        assert cmd[:3] == ["python", "/config/packs/dispatch/handler.py", "dispatch_issue"]
        assert "--agent" in cmd and cmd[cmd.index("--agent") + 1] == "sam"
        assert "--persona" in cmd and cmd[cmd.index("--persona") + 1] == "postmortem"
        assert "--budget-seconds" in cmd and cmd[cmd.index("--budget-seconds") + 1] == "900"
        assert "--supervision-mode" in cmd and cmd[cmd.index("--supervision-mode") + 1] == "poll"
        assert "--approved" in cmd
        assert "--issue-url" in cmd and cmd[cmd.index("--issue-url") + 1] == request["issue_url"]

        assert "--exec" in cmd
        exec_cmd = cmd[cmd.index("--exec") + 1 :]
        assert exec_cmd[0] == "claude"
        assert "-p" in exec_cmd
        prompt = exec_cmd[exec_cmd.index("-p") + 1]
        assert request["dispatch_id"] in prompt
        assert request["workspace_path"] in prompt
        assert "salvage" in prompt.lower()
        assert "re-dispatch" in prompt.lower()
        assert "--add-dir" in exec_cmd and exec_cmd[exec_cmd.index("--add-dir") + 1] == request["workspace_path"]

    def test_prompt_forbids_self_re_dispatch_and_broad_pytest(self):
        prompt = post_mortem.build_prompt(_request())
        assert "do not" in prompt.lower()
        assert "re-dispatch anything yourself" in prompt.lower()
        assert "whole test suite" in prompt.lower()


@pytest.mark.asyncio
class TestDrainQueue:
    async def test_drain_dispatches_and_removes_from_queue_when_cap_allows(self, tmp_path):
        payload = _payload(tmp_path)
        post_mortem.enqueue(
            "disp-dead-1",
            issue_url="https://github.com/o/r/issues/900",
            workspace_path="/var/lib/dispatch/disp-dead-1",
            reason="budget_overrun",
            elapsed_seconds=1810,
            budget_seconds=1800,
            channel="C123",
            thread_ts="1.0",
            dispatch_root=str(tmp_path),
        )
        assert len(post_mortem.read_queue(str(tmp_path))) == 1

        dispatch_mock = AsyncMock(return_value="launched")
        with (
            patch("router.auto_dispatch.loop._dispatch_post_mortem_worker", dispatch_mock),
            patch("router.auto_dispatch.loop._count_in_flight_dispatches", return_value=0),
        ):
            await _drain_post_mortem_queue(payload=payload, cfg={"max_concurrent_workers_per_login": 1})

        dispatch_mock.assert_awaited_once()
        request_arg = dispatch_mock.await_args.args[0]
        assert request_arg["dispatch_id"] == "disp-dead-1"
        assert post_mortem.read_queue(str(tmp_path)) == []

    async def test_drain_leaves_request_queued_when_cap_is_full(self, tmp_path):
        payload = _payload(tmp_path)
        post_mortem.enqueue(
            "disp-dead-1",
            issue_url="https://github.com/o/r/issues/900",
            workspace_path="/var/lib/dispatch/disp-dead-1",
            reason="budget_overrun",
            elapsed_seconds=1810,
            budget_seconds=1800,
            channel="C123",
            thread_ts="1.0",
            dispatch_root=str(tmp_path),
        )

        dispatch_mock = AsyncMock(return_value="launched")
        with (
            patch("router.auto_dispatch.loop._dispatch_post_mortem_worker", dispatch_mock),
            patch("router.auto_dispatch.loop._count_in_flight_dispatches", return_value=1),
        ):
            await _drain_post_mortem_queue(payload=payload, cfg={"max_concurrent_workers_per_login": 1})

        dispatch_mock.assert_not_awaited()
        queued = post_mortem.read_queue(str(tmp_path))
        assert len(queued) == 1
        assert queued[0]["dispatch_id"] == "disp-dead-1"

    async def test_drain_suppressed_when_breaker_tripped(self, tmp_path):
        payload = _payload(tmp_path)
        post_mortem.enqueue(
            "disp-dead-1",
            issue_url="https://github.com/o/r/issues/900",
            workspace_path="/var/lib/dispatch/disp-dead-1",
            reason="budget_overrun",
            elapsed_seconds=1810,
            budget_seconds=1800,
            channel="C123",
            thread_ts="1.0",
            dispatch_root=str(tmp_path),
        )
        trip(_breaker_path(payload), reason="signed_out", now_ts=1.0)

        dispatch_mock = AsyncMock(return_value="launched")
        with patch("router.auto_dispatch.loop._dispatch_post_mortem_worker", dispatch_mock):
            await _drain_post_mortem_queue(payload=payload, cfg={"max_concurrent_workers_per_login": 1})

        dispatch_mock.assert_not_awaited()
        assert len(post_mortem.read_queue(str(tmp_path))) == 1

    async def test_drain_noop_when_queue_empty(self, tmp_path):
        payload = _payload(tmp_path)
        dispatch_mock = AsyncMock(return_value="launched")
        with patch("router.auto_dispatch.loop._dispatch_post_mortem_worker", dispatch_mock):
            await _drain_post_mortem_queue(payload=payload, cfg={"max_concurrent_workers_per_login": 1})
        dispatch_mock.assert_not_awaited()
