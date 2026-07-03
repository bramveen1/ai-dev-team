"""Tests for router.commands.core — the transport-neutral command dispatcher.

Covers:
* Human-only gate: bot principals rejected for all mutating verbs
* Human-only gate: help is always allowed (for any principal kind)
* dispatch_command routes help → help text
* dispatch_command routes kill/killall → kill handler
* dispatch_command routes tasks list → tasks handler
* Bot @Agent aidt killall regression: is a no-op and returns the rejection
* Principal type: kind field drives the gate

Design invariant (from issue #658):
  The human-only gate lives in core so it cannot be bypassed by a new
  transport shim that forgets the check.  Agents cannot run commands —
  the emergency-stop (killall) stays human-only even if agent output
  contains the keyword.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from router.commands.core import HUMAN_ONLY_VERBS, dispatch_command
from router.commands.grammar import parse
from router.commands.types import Principal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_dispatch_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))


def _human(ref: str = "discord:human-1") -> Principal:
    return Principal(ref=ref, kind="human")


def _bot(ref: str = "discord:bot-1") -> Principal:
    return Principal(ref=ref, kind="bot")


# ---------------------------------------------------------------------------
# Principal type
# ---------------------------------------------------------------------------


class TestPrincipalType:
    def test_human_kind(self):
        p = Principal(ref="discord:123", kind="human")
        assert p.kind == "human"
        assert p.ref == "discord:123"

    def test_bot_kind(self):
        p = Principal(ref="discord:bot", kind="bot")
        assert p.kind == "bot"

    def test_principal_is_frozen(self):
        p = Principal(ref="x", kind="human")
        with pytest.raises(Exception):
            p.kind = "bot"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# HUMAN_ONLY_VERBS set
# ---------------------------------------------------------------------------


class TestHumanOnlyVerbsSet:
    def test_kill_in_human_only(self):
        assert "kill" in HUMAN_ONLY_VERBS

    def test_killall_in_human_only(self):
        assert "killall" in HUMAN_ONLY_VERBS

    def test_tasks_in_human_only(self):
        assert "tasks" in HUMAN_ONLY_VERBS

    def test_help_not_in_human_only(self):
        assert "help" not in HUMAN_ONLY_VERBS

    def test_grant_in_human_only(self):
        assert "grant" in HUMAN_ONLY_VERBS

    def test_revoke_in_human_only(self):
        assert "revoke" in HUMAN_ONLY_VERBS


# ---------------------------------------------------------------------------
# Human-only gate — bot principal rejected
# ---------------------------------------------------------------------------


class TestHumanOnlyGate:
    @pytest.mark.asyncio
    async def test_bot_kill_rejected(self):
        cmd = parse("kill", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok
        assert "agents cannot run commands" in result.text or "restricted" in result.text

    @pytest.mark.asyncio
    async def test_bot_killall_rejected(self):
        """Regression: bot @Agent aidt killall → no-op, returns rejection."""
        cmd = parse("killall", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok
        assert "restricted" in result.text or "agents cannot run commands" in result.text

    @pytest.mark.asyncio
    async def test_bot_tasks_rejected(self):
        cmd = parse("tasks list", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_grant_rejected(self):
        cmd = parse("grant github", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_revoke_rejected(self):
        cmd = parse("revoke github", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_list_packs_rejected(self):
        cmd = parse("list packs", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_who_has_rejected(self):
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok

    @pytest.mark.asyncio
    async def test_rejection_message_identifies_verb(self):
        cmd = parse("killall", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert "killall" in result.text

    @pytest.mark.asyncio
    async def test_bot_help_is_allowed(self):
        """help is exempt from the human-only gate."""
        cmd = parse("help", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert result.ok
        assert result.text  # non-empty help text

    @pytest.mark.asyncio
    async def test_human_killall_passes_gate(self):
        """Gate passes for a human principal — the kill logic runs."""
        cmd = parse("killall", transport="discord", conversation_ref="discord:1:100:0")
        with patch("router.kill_command.get_agent_map", return_value={}):
            with patch("router.kill_command.get_default_guard") as mock_guard_fn:
                from router.stuck_guard import GuardConfig, StuckGuard

                guard = StuckGuard(config=GuardConfig(mode="dry-run"))
                mock_guard_fn.return_value = guard
                result = await dispatch_command(cmd, _human())
        assert result.ok or "No active" in result.text


# ---------------------------------------------------------------------------
# dispatch_command routing
# ---------------------------------------------------------------------------


class TestDispatchCommandRouting:
    @pytest.mark.asyncio
    async def test_help_returns_help_text(self):
        cmd = parse("help", transport="discord")
        result = await dispatch_command(cmd, _human())
        assert result.ok
        assert "kill" in result.text  # full help text contains kill verb
        assert "aidt" in result.text

    @pytest.mark.asyncio
    async def test_help_with_verb_arg(self):
        cmd = parse("help kill", transport="discord")
        result = await dispatch_command(cmd, _human())
        assert result.ok
        assert "kill" in result.text

    @pytest.mark.asyncio
    async def test_kill_dispatches_to_kill_handler(self):
        """kill verb passes gate and reaches execute_kill_command."""
        cmd = parse("kill", transport="discord", conversation_ref="discord:1:100:42")
        with patch("router.kill_command.get_agent_map", return_value={"sam": {}}):
            with patch("router.kill_command.get_default_guard") as mock_guard_fn:
                from router.stuck_guard import GuardConfig, StuckGuard

                guard = StuckGuard(config=GuardConfig(mode="dry-run"))
                mock_guard_fn.return_value = guard
                result = await dispatch_command(
                    cmd, _human(), subject_agent="sam", active_agent_resolver=lambda _c, _t: "sam"
                )
        # kill in thread 42 — may say killed or no-active depending on guard state
        assert result.text

    @pytest.mark.asyncio
    async def test_tasks_list_dispatches_to_tasks_handler(self):
        """tasks list passes gate and reaches execute_tasks_command."""
        cmd = parse("tasks list", transport="discord")
        mock_store = MagicMock()
        mock_store.list_for_agent = MagicMock(return_value=[])
        result = await dispatch_command(cmd, _human(), subject_agent="sam", tasks_store=mock_store)
        assert result.ok
        assert "sam" in result.text.lower()

    @pytest.mark.asyncio
    async def test_tasks_list_with_tasks(self):
        cmd = parse("tasks list", transport="discord")
        mock_task = MagicMock()
        mock_task.name = "Daily standup"
        mock_task.task_id = "abc-123"
        mock_task.schedule_cron = "0 9 * * *"
        mock_task.enabled = True
        mock_store = MagicMock()
        mock_store.list_for_agent = MagicMock(return_value=[mock_task])
        result = await dispatch_command(cmd, _human(), subject_agent="sam", tasks_store=mock_store)
        assert result.ok
        assert "Daily standup" in result.text

    @pytest.mark.asyncio
    async def test_tasks_list_no_store_returns_error(self):
        cmd = parse("tasks list", transport="discord")
        result = await dispatch_command(cmd, _human(), subject_agent="sam", tasks_store=None)
        assert not result.ok


# ---------------------------------------------------------------------------
# Regression: bot-authored aidt killall end-to-end
# ---------------------------------------------------------------------------


class TestBotKillallRegression:
    """Issue #658 acceptance criterion:

    An agent-authored (bot) ``@Agent aidt killall`` MUST be a no-op and return
    the rejection.  The emergency-stop stays human-only even if agent output
    contains the keyword.
    """

    @pytest.mark.asyncio
    async def test_bot_aidt_killall_is_noop(self):
        """Simulate full path: Discord message → shim → dispatch_command."""
        from router.commands.discord_shim import parse_from_discord_message

        # A bot sends "@Agent aidt killall" (simulate agent looping killall).
        text = "<@111> aidt killall"
        cmd = parse_from_discord_message(text, conversation_ref="discord:1:100:0")
        assert cmd is not None
        assert cmd.verb == "killall"

        bot_principal = Principal(ref="discord:bot-agent", kind="bot")
        result = await dispatch_command(cmd, bot_principal)

        # Must be rejected — not executed.
        assert not result.ok
        assert "restricted" in result.text or "agents cannot run commands" in result.text

    @pytest.mark.asyncio
    async def test_bot_aidt_killall_does_not_touch_guard(self):
        """The guard must not be called at all for a bot-issued killall."""
        from router.commands.discord_shim import parse_from_discord_message

        text = "aidt killall"
        cmd = parse_from_discord_message(text)
        assert cmd is not None

        bot_principal = Principal(ref="discord:bot-agent", kind="bot")

        with patch("router.kill_command.get_default_guard") as mock_guard_fn:
            result = await dispatch_command(cmd, bot_principal)
            mock_guard_fn.assert_not_called()

        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_aidt_kill_does_not_touch_guard(self):
        """The guard must not be called at all for a bot-issued kill."""
        from router.commands.discord_shim import parse_from_discord_message

        text = "aidt kill"
        cmd = parse_from_discord_message(text)
        assert cmd is not None

        bot_principal = Principal(ref="discord:bot-agent", kind="bot")

        with patch("router.kill_command.get_default_guard") as mock_guard_fn:
            result = await dispatch_command(cmd, bot_principal)
            mock_guard_fn.assert_not_called()

        assert not result.ok


# ---------------------------------------------------------------------------
# approve / reject — human-only gate + dispatch routing
# ---------------------------------------------------------------------------


class TestApproveRejectDispatch:
    @pytest.mark.asyncio
    async def test_approve_in_human_only_verbs(self):
        """approve is restricted to human principals."""
        from router.commands.core import HUMAN_ONLY_VERBS

        assert "approve" in HUMAN_ONLY_VERBS

    @pytest.mark.asyncio
    async def test_reject_in_human_only_verbs(self):
        """reject is restricted to human principals."""
        from router.commands.core import HUMAN_ONLY_VERBS

        assert "reject" in HUMAN_ONLY_VERBS

    @pytest.mark.asyncio
    async def test_bot_approve_rejected(self):
        cmd = parse("approve", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok
        assert "restricted" in result.text or "agents cannot run commands" in result.text

    @pytest.mark.asyncio
    async def test_bot_reject_rejected(self):
        cmd = parse("reject", transport="discord")
        result = await dispatch_command(cmd, _bot())
        assert not result.ok

    @pytest.mark.asyncio
    async def test_approve_no_store_returns_error(self):
        cmd = parse("approve", transport="discord")
        result = await dispatch_command(cmd, _human(), draft_store=None)
        assert not result.ok
        assert "error" in result.text.lower()

    @pytest.mark.asyncio
    async def test_reject_no_store_returns_error(self):
        cmd = parse("reject", transport="discord")
        result = await dispatch_command(cmd, _human(), draft_store=None)
        assert not result.ok

    @pytest.mark.asyncio
    async def test_approve_explicit_id_no_pending_draft(self):
        """approve <id> with an unknown id → error."""
        cmd = parse("approve draft-999", transport="discord")
        mock_store = MagicMock()
        mock_store.get = MagicMock(return_value=None)
        result = await dispatch_command(cmd, _human(), draft_store=mock_store)
        assert not result.ok
        assert "draft-999" in result.text

    @pytest.mark.asyncio
    async def test_approve_no_pending_in_thread(self):
        """approve (bare) with no pending drafts in conversation → error."""
        cmd = parse("approve", transport="discord", conversation_ref="discord:g:c:t")
        mock_store = MagicMock()
        mock_store.list_pending_for_conversation = MagicMock(return_value=[])
        result = await dispatch_command(cmd, _human(), draft_store=mock_store)
        assert not result.ok
        assert "no pending" in result.text

    @pytest.mark.asyncio
    async def test_approve_multiple_pending_ambiguity_error(self):
        """approve (bare) with multiple pending drafts → ambiguity error listing ids."""
        cmd = parse("approve", transport="discord", conversation_ref="discord:g:c:t")
        draft1 = MagicMock()
        draft1.draft_id = "draft-aaa"
        draft2 = MagicMock()
        draft2.draft_id = "draft-bbb"
        mock_store = MagicMock()
        mock_store.list_pending_for_conversation = MagicMock(return_value=[draft1, draft2])
        result = await dispatch_command(cmd, _human(), draft_store=mock_store)
        assert not result.ok
        assert "draft-aaa" in result.text
        assert "draft-bbb" in result.text

    @pytest.mark.asyncio
    async def test_approve_single_pending_acts_on_it(self):
        """approve (bare) with exactly one pending draft → approved."""
        cmd = parse("approve", transport="discord", conversation_ref="discord:g:c:t")
        draft1 = MagicMock()
        draft1.draft_id = "draft-xyz"
        draft1.status = "pending"
        updated = MagicMock()
        updated.draft_id = "draft-xyz"
        mock_store = MagicMock()
        mock_store.list_pending_for_conversation = MagicMock(return_value=[draft1])
        with patch("router.approvals.core.on_approval", return_value=updated) as mock_approve:
            result = await dispatch_command(cmd, _human(), draft_store=mock_store)
        mock_approve.assert_called_once_with("draft-xyz", "approved", mock_store)
        assert result.ok
        assert "draft-xyz" in result.text

    @pytest.mark.asyncio
    async def test_reject_single_pending_acts_on_it(self):
        """reject (bare) with exactly one pending draft → discarded."""
        cmd = parse("reject", transport="discord", conversation_ref="discord:g:c:t")
        draft1 = MagicMock()
        draft1.draft_id = "draft-abc"
        draft1.status = "pending"
        updated = MagicMock()
        updated.draft_id = "draft-abc"
        mock_store = MagicMock()
        mock_store.list_pending_for_conversation = MagicMock(return_value=[draft1])
        with patch("router.approvals.core.on_approval", return_value=updated) as mock_approve:
            result = await dispatch_command(cmd, _human(), draft_store=mock_store)
        mock_approve.assert_called_once_with("draft-abc", "discarded", mock_store)
        assert result.ok

    @pytest.mark.asyncio
    async def test_approve_explicit_id_success(self):
        """approve <id> where draft exists and is pending → approved."""
        cmd = parse("approve draft-explicit", transport="discord")
        draft1 = MagicMock()
        draft1.draft_id = "draft-explicit"
        draft1.status = "pending"
        updated = MagicMock()
        updated.draft_id = "draft-explicit"
        mock_store = MagicMock()
        mock_store.get = MagicMock(return_value=draft1)
        with patch("router.approvals.core.on_approval", return_value=updated) as mock_approve:
            result = await dispatch_command(cmd, _human(), draft_store=mock_store)
        mock_approve.assert_called_once_with("draft-explicit", "approved", mock_store)
        assert result.ok
        assert "draft-explicit" in result.text
