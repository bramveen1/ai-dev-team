"""Unit tests for router.merge_queue — idle auto-merge scheduled task (#437)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import merge_queue
from router.merge_queue import (
    MERGE_IDENTITY,
    MERGEABILITY_POLL_ATTEMPTS,
    REQUIRED_CHECKS,
    TokenError,
    _is_pr_approved,
    _read_pat,
    _required_checks_passed,
    _squash_merge,
    _update_branch,
    _verify_merged,
    is_system_idle,
    register_merge_queue,
    tick,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def now():
    return datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture
def sample_pr():
    return {
        "number": 10,
        "title": "Add feature X",
        "state": "open",
        "mergeable_state": "clean",
        "user": {"login": "alice"},
        "labels": [],
        "head": {"sha": "abc123"},
        "base": {"ref": "main"},
    }


@pytest.fixture
def sample_pr_behind(sample_pr):
    return {**sample_pr, "mergeable_state": "behind"}


@pytest.fixture
def store(tmp_path):
    from router.scheduled_tasks.store import ScheduledTaskStore

    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# _read_pat
# ---------------------------------------------------------------------------


class TestReadPat:
    def test_reads_valid_token(self, tmp_path):
        pat_file = tmp_path / "token"
        pat_file.write_text("ghp_validtoken123\n")
        assert _read_pat(str(pat_file)) == "ghp_validtoken123"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(TokenError, match="not found"):
            _read_pat(str(tmp_path / "nonexistent"))

    def test_raises_on_empty_file(self, tmp_path):
        pat_file = tmp_path / "token"
        pat_file.write_text("   \n")
        with pytest.raises(TokenError, match="empty"):
            _read_pat(str(pat_file))


# ---------------------------------------------------------------------------
# is_system_idle
# ---------------------------------------------------------------------------


class TestIsSystemIdle:
    def test_idle_when_no_dispatches_and_no_sessions(self, tmp_path, now):
        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = []
            idle, reason = is_system_idle(now=now, dispatch_root_override=str(tmp_path))
        assert idle is True
        assert reason is None

    def test_not_idle_when_active_dispatch(self, tmp_path, now):
        dispatch_id = "disp-active"
        (tmp_path / dispatch_id).mkdir()
        # No exitcode file → dispatch is still running
        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = []
            idle, reason = is_system_idle(now=now, dispatch_root_override=str(tmp_path))
        assert idle is False
        assert "active_dispatch" in reason

    def test_not_idle_when_recent_completion(self, tmp_path, now):
        dispatch_id = "disp-done"
        dispatch_dir = tmp_path / dispatch_id
        dispatch_dir.mkdir()
        exitcode_path = dispatch_dir / "exitcode"
        exitcode_path.write_text("0")
        # Advance now 30s past the file's real mtime — still inside the 600s window.
        real_mtime = exitcode_path.stat().st_mtime
        recent_now = datetime.fromtimestamp(real_mtime + 30, tz=timezone.utc)
        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = []
            idle, reason = is_system_idle(now=recent_now, dispatch_root_override=str(tmp_path))
        assert idle is False
        assert "recent_completion" in reason

    def test_idle_when_completion_is_old(self, tmp_path, now):
        dispatch_id = "disp-old"
        dispatch_dir = tmp_path / dispatch_id
        dispatch_dir.mkdir()
        exitcode_path = dispatch_dir / "exitcode"
        exitcode_path.write_text("0")
        # Simulate old mtime by using a now far in the future (> 600s window)
        future_now = datetime.fromtimestamp(exitcode_path.stat().st_mtime + 700, tz=timezone.utc)
        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = []
            idle, reason = is_system_idle(now=future_now, dispatch_root_override=str(tmp_path))
        assert idle is True

    def test_not_idle_when_active_session(self, tmp_path, now):
        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = [{"session_id": "s1"}]
            idle, reason = is_system_idle(now=now, dispatch_root_override=str(tmp_path))
        assert idle is False
        assert reason == "active_conversation"


# ---------------------------------------------------------------------------
# _is_pr_approved
# ---------------------------------------------------------------------------


class TestIsPrApproved:
    @pytest.mark.asyncio
    async def test_approved_by_non_author_review(self, sample_pr):
        reviews = [{"state": "APPROVED", "user": {"login": "bob"}}]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = reviews
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _is_pr_approved("org/repo", 10, sample_pr, "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_not_approved_when_only_author_review(self, sample_pr):
        reviews = [{"state": "APPROVED", "user": {"login": "alice"}}]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = reviews
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _is_pr_approved("org/repo", 10, sample_pr, "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_approved_via_auto_merge_label(self, sample_pr):
        pr = {**sample_pr, "labels": [{"name": "auto-merge"}]}
        result = await _is_pr_approved("org/repo", 10, pr, "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_raises_token_error_on_401(self, sample_pr):
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_get.return_value = mock_resp
            with pytest.raises(TokenError):
                await _is_pr_approved("org/repo", 10, sample_pr, "bad_tok")


# ---------------------------------------------------------------------------
# _required_checks_passed
# ---------------------------------------------------------------------------


class TestRequiredChecksPassed:
    def _make_run(self, name: str, conclusion: str) -> dict:
        return {"name": name, "conclusion": conclusion}

    @pytest.mark.asyncio
    async def test_all_checks_pass(self):
        runs = [self._make_run(name, "success") for name in REQUIRED_CHECKS]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"check_runs": runs}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_missing_check_fails(self):
        checks = list(REQUIRED_CHECKS)
        runs = [self._make_run(name, "success") for name in checks[:-1]]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"check_runs": runs}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_failing_check_fails(self):
        runs = [self._make_run(name, "success") for name in REQUIRED_CHECKS]
        runs[0]["conclusion"] = "failure"
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"check_runs": runs}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is False


# ---------------------------------------------------------------------------
# _update_branch
# ---------------------------------------------------------------------------


class TestUpdateBranch:
    @pytest.mark.asyncio
    async def test_returns_true_on_202(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=202)
            mock_resp.raise_for_status = MagicMock()
            mock_put.return_value = mock_resp
            result = await _update_branch("org/repo", 10, "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_conflict(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=422)
            mock_resp.raise_for_status = MagicMock()
            mock_put.return_value = mock_resp
            result = await _update_branch("org/repo", 10, "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_raises_token_error_on_401(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=401)
            mock_put.return_value = mock_resp
            with pytest.raises(TokenError):
                await _update_branch("org/repo", 10, "bad")


# ---------------------------------------------------------------------------
# _squash_merge
# ---------------------------------------------------------------------------


class TestSquashMerge:
    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=200)
            mock_put.return_value = mock_resp
            result = await _squash_merge("org/repo", 10, "My PR", "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_405(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=405, text="not mergeable")
            mock_put.return_value = mock_resp
            result = await _squash_merge("org/repo", 10, "My PR", "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_raises_token_error_on_401(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=401)
            mock_put.return_value = mock_resp
            with pytest.raises(TokenError):
                await _squash_merge("org/repo", 10, "My PR", "bad")


# ---------------------------------------------------------------------------
# _verify_merged
# ---------------------------------------------------------------------------


class TestVerifyMerged:
    @pytest.mark.asyncio
    async def test_returns_true_when_verified(self):
        closed_pr = {
            "state": "closed",
            "merged": True,
            "merged_by": {"login": MERGE_IDENTITY},
            "mergeable_state": "merged",
        }
        with patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=closed_pr)):
            result = await _verify_merged("org/repo", 10, "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_still_open(self):
        pr = {"state": "open", "merged": False, "merged_by": None}
        with patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr)):
            result = await _verify_merged("org/repo", 10, "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_wrong_identity(self):
        pr = {
            "state": "closed",
            "merged": True,
            "merged_by": {"login": "other-user"},
        }
        with patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr)):
            result = await _verify_merged("org/repo", 10, "tok")
        assert result is False


# ---------------------------------------------------------------------------
# tick — full flow tests
# ---------------------------------------------------------------------------


def _make_payload(tmp_path: Path) -> dict:
    pat_file = tmp_path / "token"
    pat_file.write_text("ghp_test_token")
    return {
        "repo": "org/repo",
        "pat_path": str(pat_file),
        "destination": "C_NOTIFY",
    }


@pytest.mark.asyncio
class TestTick:
    async def test_skips_when_no_repo_in_payload(self, slack_client, now):
        result = await tick(payload={}, slack_client=slack_client, now=now)
        assert result["skipped"] == "no_repo"

    async def test_skips_on_missing_token(self, tmp_path, slack_client, now):
        payload = {
            "repo": "org/repo",
            "pat_path": str(tmp_path / "nonexistent"),
            "destination": "C_NOTIFY",
        }
        result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "token_error"
        slack_client.chat_postMessage.assert_awaited_once()

    async def test_skips_when_not_idle(self, tmp_path, slack_client, now):
        payload = _make_payload(tmp_path)
        with patch("router.merge_queue.is_system_idle", return_value=(False, "active_conversation")):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "active_conversation"

    async def test_idle_guard_threads_configured_session_timeout(self, tmp_path, slack_client, now, monkeypatch):
        """tick must forward the configured SESSION_TIMEOUT to is_system_idle.

        Regression for PR #528 / issue #462: is_system_idle gained a
        ``session_timeout`` param but the caller never passed it, leaving the
        merge-queue idle view on the DEFAULT boundary and ignoring the
        configured/env value. The dead param is now wired.
        """
        payload = _make_payload(tmp_path)
        monkeypatch.setenv("SESSION_TIMEOUT", "1234")
        idle_mock = MagicMock(return_value=(False, "active_conversation"))
        with patch("router.merge_queue.is_system_idle", idle_mock):
            await tick(payload=payload, slack_client=slack_client, now=now)
        idle_mock.assert_called_once()
        assert idle_mock.call_args.kwargs["session_timeout"] == 1234

    async def test_idle_guard_session_timeout_falls_back_to_default(self, tmp_path, slack_client, now, monkeypatch):
        """With no SESSION_TIMEOUT env, tick forwards the DEFAULTS value."""
        from router.config import DEFAULTS

        payload = _make_payload(tmp_path)
        monkeypatch.delenv("SESSION_TIMEOUT", raising=False)
        idle_mock = MagicMock(return_value=(False, "active_conversation"))
        with patch("router.merge_queue.is_system_idle", idle_mock):
            await tick(payload=payload, slack_client=slack_client, now=now)
        assert idle_mock.call_args.kwargs["session_timeout"] == DEFAULTS["session_timeout"]

    async def test_skips_when_no_open_prs(self, tmp_path, slack_client, now):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[])),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "no_open_prs"

    async def test_updates_branch_when_behind(self, tmp_path, slack_client, now, sample_pr_behind):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr_behind])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr_behind)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)) as mock_update,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "branch_updated"
        assert result["pr"] == sample_pr_behind["number"]
        mock_update.assert_awaited_once()

    async def test_posts_slack_on_update_branch_conflict(self, tmp_path, slack_client, now, sample_pr_behind):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr_behind])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr_behind)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=False)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "branch_updated"
        slack_client.chat_postMessage.assert_awaited_once()
        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert "rebase" in call_kwargs["text"]
        assert f"#{sample_pr_behind['number']}" in call_kwargs["text"]

    async def test_skips_non_clean_mergeable_state(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        blocked_pr = {**sample_pr, "mergeable_state": "blocked"}
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[blocked_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=blocked_pr)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "not_mergeable"

    async def test_skips_when_ci_not_green(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=False)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "ci_not_green"

    async def test_skips_when_not_approved(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=False)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "not_approved"

    async def test_merges_approved_green_pr(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        pr_num = sample_pr["number"]
        pr2 = {**sample_pr, "number": 11}

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr, pr2])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)) as mock_update,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "merged"
        assert result["pr"] == pr_num
        # Next head should have been update-branched
        mock_update.assert_awaited_once_with("org/repo", pr2["number"], "ghp_test_token")

    async def test_no_update_branch_when_single_pr(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._update_branch", new=AsyncMock()) as mock_update,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "merged"
        mock_update.assert_not_awaited()

    async def test_merge_refused_by_github(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=False)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "merge_refused"

    async def test_unverified_merge_reported(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=False)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "merge_unverified"

    async def test_token_error_posts_to_slack(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", side_effect=TokenError("401 on list")),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["skipped"] == "token_error"
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert ":x:" in text


# ---------------------------------------------------------------------------
# tick — unknown mergeability polling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTickUnknownMergeability:
    async def test_unknown_then_clean_proceeds_to_merge(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        unknown_pr = {**sample_pr, "mergeable_state": "unknown"}
        clean_pr = {**sample_pr, "mergeable_state": "clean"}
        details_mock = AsyncMock(side_effect=[unknown_pr, clean_pr])

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=details_mock),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)),
            patch("router.merge_queue.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "merged"
        assert result["pr"] == sample_pr["number"]
        mock_sleep.assert_awaited_once_with(merge_queue.MERGEABILITY_POLL_INTERVAL_S)

    async def test_unknown_then_behind_proceeds_to_update_branch(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        unknown_pr = {**sample_pr, "mergeable_state": "unknown"}
        behind_pr = {**sample_pr, "mergeable_state": "behind"}
        details_mock = AsyncMock(side_effect=[unknown_pr, behind_pr])

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=details_mock),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)) as mock_update,
            patch("router.merge_queue.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "branch_updated"
        assert result["pr"] == sample_pr["number"]
        mock_sleep.assert_awaited_once_with(merge_queue.MERGEABILITY_POLL_INTERVAL_S)
        mock_update.assert_awaited_once()

    async def test_unknown_for_all_attempts_skips_not_mergeable(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        unknown_pr = {**sample_pr, "mergeable_state": "unknown"}

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=unknown_pr)),
            patch("router.merge_queue.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["skipped"] == "not_mergeable"
        assert result["pr"] == sample_pr["number"]
        assert mock_sleep.await_count == MERGEABILITY_POLL_ATTEMPTS


# ---------------------------------------------------------------------------
# register_merge_queue
# ---------------------------------------------------------------------------


class TestRegisterMergeQueue:
    def test_registers_new_task(self, store):
        task = register_merge_queue(store, agent_name="sam", repo="org/repo")
        assert task.callable_ref == merge_queue.CALLABLE_REF
        assert task.payload["repo"] == "org/repo"
        assert task.period_seconds == merge_queue.DEFAULT_PERIOD_SECONDS

    def test_idempotent_on_second_call(self, store):
        t1 = register_merge_queue(store, agent_name="sam", repo="org/repo")
        t2 = register_merge_queue(store, agent_name="sam", repo="org/repo")
        assert t1.task_id == t2.task_id
        assert len(store.list_by_callable_ref(merge_queue.CALLABLE_REF)) == 1

    def test_stores_pat_path(self, store):
        task = register_merge_queue(store, agent_name="sam", repo="org/repo", pat_path="/custom/path")
        assert task.payload["pat_path"] == "/custom/path"

    def test_stores_destination_when_provided(self, store):
        task = register_merge_queue(store, agent_name="sam", repo="org/repo", destination="C_BRAM")
        assert task.payload["destination"] == "C_BRAM"

    def test_no_destination_key_when_omitted(self, store):
        task = register_merge_queue(store, agent_name="sam", repo="org/repo")
        assert "destination" not in task.payload
