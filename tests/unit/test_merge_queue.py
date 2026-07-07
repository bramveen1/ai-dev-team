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

    def test_idle_when_dispatch_dir_has_no_heartbeat_and_is_old(self, tmp_path, now):
        """A ghost slot (no exitcode, no heartbeat, old mtime) must not suppress idle.

        This is the core AC from issue #612: a force-killed worker leaves its
        dir on disk; the idle check must reap it rather than blocking merges.
        """
        import os
        import time

        dispatch_id = "dispatch-20260629T153818-dead0a"
        d = tmp_path / dispatch_id
        d.mkdir()
        # No exitcode, no heartbeat; mtime well past MAX_DISPATCH_AGE_SECONDS.
        old_ts = time.time() - 8000
        os.utime(d, (old_ts, old_ts))

        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = []
            idle, reason = is_system_idle(dispatch_root_override=str(tmp_path))

        assert idle is True, f"expected idle but got reason={reason!r}"
        # Ghost slot must have been reaped.
        assert not d.exists()
        orphans = list((tmp_path / "_orphans").iterdir())
        assert len(orphans) == 1
        assert dispatch_id in orphans[0].name

    def test_not_idle_when_dispatch_has_fresh_heartbeat(self, tmp_path, now):
        """A slot with a fresh heartbeat is genuinely alive; idle must return False."""
        import os
        import time

        dispatch_id = "dispatch-20260630T000000-alive0"
        d = tmp_path / dispatch_id
        d.mkdir()
        # Backdate the dir but write a fresh heartbeat.
        old_ts = time.time() - 120
        os.utime(d, (old_ts, old_ts))
        hb = d / "heartbeat"
        hb.write_text("alive")
        # heartbeat mtime ≈ now → alive

        with patch("router.merge_queue.session_manager") as mock_sm:
            mock_sm.get_active_sessions.return_value = []
            idle, reason = is_system_idle(dispatch_root_override=str(tmp_path))

        assert idle is False
        assert "active_dispatch" in reason


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

    @pytest.mark.asyncio
    async def test_blocked_when_reviewer_approves_then_requests_changes(self, sample_pr):
        """Stale APPROVED is overridden by a later CHANGES_REQUESTED from the same reviewer."""
        reviews = [
            {"state": "APPROVED", "user": {"login": "bob"}},
            {"state": "CHANGES_REQUESTED", "user": {"login": "bob"}},
        ]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = reviews
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _is_pr_approved("org/repo", 10, sample_pr, "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_approved_when_reviewer_requests_changes_then_reapproves(self, sample_pr):
        """A later APPROVED overrides an earlier CHANGES_REQUESTED from the same reviewer."""
        reviews = [
            {"state": "CHANGES_REQUESTED", "user": {"login": "bob"}},
            {"state": "APPROVED", "user": {"login": "bob"}},
        ]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = reviews
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _is_pr_approved("org/repo", 10, sample_pr, "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_commented_review_does_not_affect_approval(self, sample_pr):
        """COMMENTED reviews are ignored; the earlier APPROVED still stands."""
        reviews = [
            {"state": "APPROVED", "user": {"login": "bob"}},
            {"state": "COMMENTED", "user": {"login": "bob"}},
        ]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = reviews
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _is_pr_approved("org/repo", 10, sample_pr, "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_one_approver_one_blocker_is_blocked(self, sample_pr):
        """If any non-author reviewer's latest state is CHANGES_REQUESTED, the PR is blocked."""
        reviews = [
            {"state": "APPROVED", "user": {"login": "bob"}},
            {"state": "CHANGES_REQUESTED", "user": {"login": "carol"}},
        ]
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = reviews
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _is_pr_approved("org/repo", 10, sample_pr, "tok")
        assert result is False


# ---------------------------------------------------------------------------
# _required_checks_passed
# ---------------------------------------------------------------------------


class TestRequiredChecksPassed:
    def _make_run(self, name: str, conclusion: str, run_id: int = 1) -> dict:
        return {"name": name, "conclusion": conclusion, "id": run_id}

    @pytest.mark.asyncio
    async def test_all_checks_pass(self):
        runs = [self._make_run(name, "success", i) for i, name in enumerate(REQUIRED_CHECKS, 1)]
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
        runs = [self._make_run(name, "success", i) for i, name in enumerate(checks[:-1], 1)]
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
        runs = [self._make_run(name, "success", i) for i, name in enumerate(REQUIRED_CHECKS, 1)]
        runs[0]["conclusion"] = "failure"
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"check_runs": runs}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_stale_success_ignored_when_latest_run_fails(self):
        """A later failure run must supersede an earlier success for the same check."""
        checks = list(REQUIRED_CHECKS)
        # First run of each check: success (low ids)
        runs = [self._make_run(name, "success", i + 1) for i, name in enumerate(checks)]
        # Re-run the first check with a higher id — now failure
        rerun_id = len(checks) + 10
        runs.append({"name": checks[0], "conclusion": "failure", "id": rerun_id})
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"check_runs": runs}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_latest_success_wins_over_older_failure(self):
        """When a check is re-run and the latest run succeeds, it should pass."""
        checks = list(REQUIRED_CHECKS)
        # First run of first check: failure
        runs = [{"name": checks[0], "conclusion": "failure", "id": 1}]
        # Re-run of first check: success (higher id)
        runs.append({"name": checks[0], "conclusion": "success", "id": 100})
        # Remaining checks: success
        for i, name in enumerate(checks[1:], 2):
            runs.append(self._make_run(name, "success", i))
        with patch("router.merge_queue._gh_get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"check_runs": runs}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_paginates_when_first_page_is_full(self):
        """A second page is fetched when the first page returns exactly 100 runs."""
        checks = list(REQUIRED_CHECKS)
        page1 = [{"name": f"extra-{i}", "conclusion": "success", "id": i + 1} for i in range(100)]
        page2 = [self._make_run(name, "success", 200 + i) for i, name in enumerate(checks)]

        responses = [
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"check_runs": page1}),
            ),
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"check_runs": page2}),
            ),
        ]
        with patch("router.merge_queue._gh_get", new=AsyncMock(side_effect=responses)):
            result = await _required_checks_passed("org/repo", "abc123", "tok")
        assert result is True


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
            result = await _squash_merge("org/repo", 10, "My PR", "abc123", "tok")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_405(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=405, text="not mergeable")
            mock_put.return_value = mock_resp
            result = await _squash_merge("org/repo", 10, "My PR", "abc123", "tok")
        assert result is False

    @pytest.mark.asyncio
    async def test_raises_token_error_on_401(self):
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=401)
            mock_put.return_value = mock_resp
            with pytest.raises(TokenError):
                await _squash_merge("org/repo", 10, "My PR", "abc123", "bad")

    @pytest.mark.asyncio
    async def test_body_contains_validated_sha(self):
        """The merge PUT body must include the validated head SHA (#513)."""
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=200)
            mock_put.return_value = mock_resp
            await _squash_merge("org/repo", 10, "My PR", "deadbeef", "tok")
        _, _, sent_body = mock_put.call_args.args
        assert sent_body["sha"] == "deadbeef"

    @pytest.mark.asyncio
    async def test_returns_none_on_409_head_moved(self):
        """A 409 from GitHub means the head moved; return None to signal re-validation."""
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=409, text="Head branch was modified")
            mock_put.return_value = mock_resp
            result = await _squash_merge("org/repo", 10, "My PR", "abc123", "tok")
        assert result is None

    @pytest.mark.asyncio
    async def test_commit_title_format_preserved(self):
        """Successful merge must use the existing commit_title format."""
        with patch("router.merge_queue._gh_put") as mock_put:
            mock_resp = MagicMock(status_code=200)
            mock_put.return_value = mock_resp
            await _squash_merge("org/repo", 42, "Add feature X", "abc123", "tok")
        _, _, sent_body = mock_put.call_args.args
        assert sent_body["commit_title"] == "Add feature X (#42)"
        assert sent_body["merge_method"] == "squash"


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
        """With no SESSION_TIMEOUT env, tick forwards the registry default."""
        from router.settings import REGISTRY

        payload = _make_payload(tmp_path)
        monkeypatch.delenv("SESSION_TIMEOUT", raising=False)
        idle_mock = MagicMock(return_value=(False, "active_conversation"))
        with patch("router.merge_queue.is_system_idle", idle_mock):
            await tick(payload=payload, slack_client=slack_client, now=now)
        assert idle_mock.call_args.kwargs["session_timeout"] == REGISTRY["SESSION_TIMEOUT"].default

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
        # Single ineligible PR → queue exhausted with no eligible candidate.
        assert result["skipped"] == "no_eligible_pr"

    async def test_skips_when_ci_not_green(self, tmp_path, slack_client, now, sample_pr):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=False)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        # Single ineligible PR → queue exhausted with no eligible candidate.
        assert result["skipped"] == "no_eligible_pr"

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
        # Single ineligible PR → queue exhausted with no eligible candidate.
        assert result["skipped"] == "no_eligible_pr"

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

    async def test_head_moved_409_drops_to_revalidation(self, tmp_path, slack_client, now, sample_pr):
        """When _squash_merge returns None (409), tick must drop the PR back to
        re-validation (action=head_moved) without recording a merge or raising."""
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=None)),
            patch("router.merge_queue._verify_merged") as mock_verify,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "head_moved"
        assert result["pr"] == sample_pr["number"]
        mock_verify.assert_not_called()

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
# tick — skip-ahead iteration (issue #540)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTickSkipAhead:
    """Verify that tick() skips blocked head PRs and merges the next eligible one."""

    async def test_blocked_head_eligible_second_merges(self, tmp_path, slack_client, now, sample_pr):
        """(a) Blocked head + eligible second PR → second PR merges."""
        payload = _make_payload(tmp_path)
        blocked_pr = {**sample_pr, "number": 10, "mergeable_state": "blocked"}
        eligible_pr = {**sample_pr, "number": 11, "mergeable_state": "clean", "head": {"sha": "def456"}}

        details_side_effect = [blocked_pr, eligible_pr]

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[blocked_pr, eligible_pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(side_effect=details_side_effect)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)) as mock_update,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "merged"
        assert result["pr"] == eligible_pr["number"]
        # After merging PR#11, the new head is the oldest remaining open PR → PR#10.
        mock_update.assert_awaited_once_with("org/repo", blocked_pr["number"], "ghp_test_token")

    async def test_all_prs_ineligible_nothing_merges(self, tmp_path, slack_client, now, sample_pr):
        """(b) All PRs ineligible → nothing merges; tick returns no_eligible_pr."""
        payload = _make_payload(tmp_path)
        pr1 = {**sample_pr, "number": 10, "mergeable_state": "blocked"}
        pr2 = {**sample_pr, "number": 11, "mergeable_state": "blocked"}

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr1, pr2])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(side_effect=[pr1, pr2])),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["skipped"] == "no_eligible_pr"
        assert result.get("action") is None

    async def test_eligible_head_still_merges_no_regression(self, tmp_path, slack_client, now, sample_pr):
        """(c) Eligible head → head merges (no regression from old behaviour)."""
        payload = _make_payload(tmp_path)
        pr2 = {**sample_pr, "number": 11}

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[sample_pr, pr2])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=sample_pr)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "merged"
        assert result["pr"] == sample_pr["number"]

    async def test_only_one_merge_per_tick(self, tmp_path, slack_client, now, sample_pr):
        """(d) Even when multiple PRs are eligible, at most one is merged per tick."""
        payload = _make_payload(tmp_path)
        pr1 = {**sample_pr, "number": 10, "head": {"sha": "sha10"}}
        pr2 = {**sample_pr, "number": 11, "head": {"sha": "sha11"}}

        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr1, pr2])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr1)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._is_pr_approved", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)) as mock_merge,
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)

        assert result["action"] == "merged"
        assert result["pr"] == pr1["number"]
        # Only one squash-merge call despite two eligible PRs.
        mock_merge.assert_awaited_once()


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

        # Persistently unknown → skipped (not merged); single-PR queue exhausted.
        assert result["skipped"] == "no_eligible_pr"
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
