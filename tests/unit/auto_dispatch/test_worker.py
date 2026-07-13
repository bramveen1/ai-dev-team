"""Thin dedicated tests for router.auto_dispatch.worker (#719).

worker.py had no dedicated test file — it was only exercised indirectly via
test_auto_dispatch.py's loop-level patches (mocked out, not run). This
covers the two documented outcomes of ``_dispatch_worker``: the spawn path
(handler returns 'launched') and the approval-gate path (handler returns
'approval_required', which must call ``_create_draft_fn`` rather than raise).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.auto_dispatch.worker import _dispatch_worker

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


def _patches(run_return):
    extras = MagicMock()
    extras.env = {"WORKERS_BOT_TOKEN": "xoxb-test"}
    return (
        patch("router.config.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
        patch("router.dispatcher._run_in_container", new=AsyncMock(return_value=run_return)),
        patch("router.packs.dispatch_hook.pack_cli_extras", return_value=extras),
    )


class TestDispatchWorkerSpawnPath:
    async def test_launched_status_returns_launched_without_approved_flag(self, slack_client):
        gam, run, pce = _patches(('{"status": "launched", "dispatch_id": "d1", "pid": 9}', "", 0))
        with gam, run as run_mock, pce:
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=slack_client,
                destination="C123",
                payload={"worker_agent": "sam"},
            )
        assert result == "launched"
        cmd = run_mock.await_args.kwargs["command"]
        assert "--approved" not in cmd


class TestDispatchWorkerApprovalGate:
    async def test_approval_required_calls_create_draft_fn_and_does_not_raise(self, slack_client):
        gam, run, pce = _patches(
            (
                '{"status": "approval_required", "draft_id": "abc123", '
                '"preview": {"repo": "o/r", "gate_reason": "always"}}',
                "",
                0,
            )
        )
        create_draft = AsyncMock(return_value=None)
        with gam, run, pce:
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=slack_client,
                destination="C123",
                payload={"worker_agent": "sam"},
                _create_draft_fn=create_draft,
            )
        assert result == "approval_required"
        create_draft.assert_awaited_once()
        assert create_draft.await_args.kwargs["gate_preview"]["gate_reason"] == "always"

    async def test_other_status_raises_and_skips_create_draft_fn(self, slack_client):
        gam, run, pce = _patches(('{"status": "gate_blocked", "reason": "denied"}', "", 0))
        create_draft = AsyncMock(return_value=None)
        with gam, run, pce:
            with pytest.raises(RuntimeError, match="gate_blocked"):
                await _dispatch_worker(
                    issue_url="https://github.com/o/r/issues/42",
                    issue_num=42,
                    issue_title="Fix it",
                    slack_client=slack_client,
                    destination="C123",
                    payload={"worker_agent": "sam"},
                    _create_draft_fn=create_draft,
                )
        create_draft.assert_not_awaited()
