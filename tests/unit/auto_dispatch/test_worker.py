"""Thin dedicated tests for router.auto_dispatch.worker (#719).

worker.py had no dedicated test file — it was only exercised incidentally
via loop-level patches in test_auto_dispatch.py (mocked out, not run). These
cover the two outcomes ``_dispatch_worker`` must distinguish cleanly: a
worker launch, and the human approval-gate firing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.auto_dispatch.worker import _dispatch_worker

pytestmark = pytest.mark.unit


def _patches(run_return):
    extras = MagicMock()
    extras.env = {"WORKERS_BOT_TOKEN": "xoxb-test"}
    return (
        patch("router.config.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
        patch("router.dispatcher._run_in_container", new=AsyncMock(return_value=run_return)),
        patch("router.packs.dispatch_hook.pack_cli_extras", return_value=extras),
    )


@pytest.mark.asyncio
class TestDispatchWorkerSpawnPath:
    async def test_launched_status_returns_launched_without_approved_flag(self):
        run_return = ('{"status": "launched", "dispatch_id": "d1", "pid": 9}', "", 0)
        gam, run_patch, pce = _patches(run_return)
        with gam, run_patch as run, pce:
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=MagicMock(),
                destination="C123",
                payload={"worker_agent": "sam"},
            )
        assert result == "launched"
        cmd = run.await_args.kwargs["command"]
        assert "--approved" not in cmd


@pytest.mark.asyncio
class TestDispatchWorkerApprovalGate:
    async def test_approval_required_invokes_create_draft_fn_and_does_not_raise(self):
        run_return = (
            '{"status": "approval_required", "draft_id": "abc123", "preview": {"gate_reason": "always"}}',
            "",
            0,
        )
        gam, run_patch, pce = _patches(run_return)
        create_draft = AsyncMock(return_value=None)
        with gam, run_patch, pce:
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=MagicMock(),
                destination="C123",
                payload={"worker_agent": "sam"},
                _create_draft_fn=create_draft,
            )
        assert result == "approval_required"
        create_draft.assert_awaited_once()
