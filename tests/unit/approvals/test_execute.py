"""Tests for router.approvals.execute — approved-draft command building.

Regression coverage for issue #798: the approval-execute command builder
must forward a staged draft's ``budget_seconds``/``persona`` to the
re-executed ``dispatch_issue`` CLI invocation, and must not append either
flag when the payload doesn't carry the key (legacy/partial drafts fall back
to the handler's own defaults).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.approvals.execute import _execute_approved_draft
from router.approvals.store import Draft

pytestmark = pytest.mark.unit


def _make_dispatch_draft(**payload_overrides) -> Draft:
    """Create a ``dispatch.dispatch_issue`` Draft with sensible defaults."""
    payload = {
        "issue_url": "https://github.com/bramveen1/ai-dev-team/issues/798",
        "repo": "bramveen1/ai-dev-team",
        "model": "opus",
    }
    payload.update(payload_overrides)
    return Draft(
        draft_id=str(uuid.uuid4()),
        agent_name="sam",
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload=payload,
        slack_channel="C12345",
        slack_message_ts="1705700000.000100",
        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


def _launched_run_in_container():
    return AsyncMock(return_value=(json.dumps({"status": "launched", "dispatch_id": "dispatch-1"}), "", 0))


class TestApprovedDispatchCommandForwarding:
    @pytest.mark.asyncio
    async def test_approved_dispatch_forwards_staged_budget_and_persona(self):
        draft = _make_dispatch_draft(budget_seconds=5400, persona="dev")
        client = AsyncMock()

        with (
            patch("router.approvals.execute.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
            patch("router.approvals.execute._workers_client", return_value=None),
            patch("router.approvals.execute.pack_cli_extras") as mock_extras,
            patch("router.approvals.execute._run_in_container", new=_launched_run_in_container()) as mock_run,
        ):
            mock_extras.return_value = MagicMock(env=None)
            await _execute_approved_draft(draft, channel="C1", thread_ts="1.0", client=client)

        assert mock_run.await_count == 1
        cmd = mock_run.await_args.kwargs["command"]
        assert "--budget-seconds" in cmd
        assert cmd[cmd.index("--budget-seconds") + 1] == "5400"
        assert "--persona" in cmd
        assert cmd[cmd.index("--persona") + 1] == "dev"

    @pytest.mark.asyncio
    async def test_approved_dispatch_omits_flags_when_absent_from_payload(self):
        draft = _make_dispatch_draft()  # no budget_seconds / persona keys
        client = AsyncMock()

        with (
            patch("router.approvals.execute.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
            patch("router.approvals.execute._workers_client", return_value=None),
            patch("router.approvals.execute.pack_cli_extras") as mock_extras,
            patch("router.approvals.execute._run_in_container", new=_launched_run_in_container()) as mock_run,
        ):
            mock_extras.return_value = MagicMock(env=None)
            await _execute_approved_draft(draft, channel="C1", thread_ts="1.0", client=client)

        cmd = mock_run.await_args.kwargs["command"]
        assert "--budget-seconds" not in cmd
        assert "--persona" not in cmd
