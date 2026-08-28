"""Unit tests for the continuous merge daemon (#832) — independent per-PR
bucket partition behind the default-off ``CONTINUOUS_MERGE`` setting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.merge_queue import (
    MAX_BRANCH_UPDATES_PER_TICK,
    SAM_LOGIN,
    SECURITY_LABEL,
    TokenError,
    _classify_pr,
    _continuous_tick,
    _sam_approved_at_head,
    tick,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def now():
    return datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


def _pr(number: int, **overrides) -> dict:
    base = {
        "number": number,
        "title": f"PR #{number}",
        "state": "open",
        "mergeable_state": "clean",
        "user": {"login": "alice"},
        "labels": [],
        "head": {"sha": f"sha-{number}"},
        "base": {"ref": "main"},
    }
    base.update(overrides)
    return base


def _make_payload(tmp_path: Path) -> dict:
    pat_file = tmp_path / "token"
    pat_file.write_text("ghp_test_token")
    return {"repo": "org/repo", "pat_path": str(pat_file), "destination": "C_NOTIFY"}


# ---------------------------------------------------------------------------
# _sam_approved_at_head
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSamApprovedAtHead:
    async def test_approved_on_current_head(self):
        reviews = [{"state": "APPROVED", "user": {"login": SAM_LOGIN}, "commit_id": "sha-1"}]
        with patch("router.merge_queue._fetch_all_reviews", new=AsyncMock(return_value=reviews)):
            result = await _sam_approved_at_head("org/repo", 1, "tok", "sha-1")
        assert result is True

    async def test_stale_approval_on_old_head_is_not_approved(self):
        """Sam approved sha-1, but HEAD moved to sha-2 — must not count (#832 invariant)."""
        reviews = [{"state": "APPROVED", "user": {"login": SAM_LOGIN}, "commit_id": "sha-1"}]
        with patch("router.merge_queue._fetch_all_reviews", new=AsyncMock(return_value=reviews)):
            result = await _sam_approved_at_head("org/repo", 1, "tok", "sha-2")
        assert result is False

    async def test_non_sam_approval_does_not_count(self):
        reviews = [{"state": "APPROVED", "user": {"login": "bob"}, "commit_id": "sha-1"}]
        with patch("router.merge_queue._fetch_all_reviews", new=AsyncMock(return_value=reviews)):
            result = await _sam_approved_at_head("org/repo", 1, "tok", "sha-1")
        assert result is False

    async def test_later_changes_requested_overrides_approval(self):
        reviews = [
            {"state": "APPROVED", "user": {"login": SAM_LOGIN}, "commit_id": "sha-1"},
            {"state": "CHANGES_REQUESTED", "user": {"login": SAM_LOGIN}, "commit_id": "sha-1"},
        ]
        with patch("router.merge_queue._fetch_all_reviews", new=AsyncMock(return_value=reviews)):
            result = await _sam_approved_at_head("org/repo", 1, "tok", "sha-1")
        assert result is False

    async def test_no_reviews_is_not_approved(self):
        with patch("router.merge_queue._fetch_all_reviews", new=AsyncMock(return_value=[])):
            result = await _sam_approved_at_head("org/repo", 1, "tok", "sha-1")
        assert result is False


# ---------------------------------------------------------------------------
# _classify_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClassifyPr:
    async def test_security_manual_label_is_bucket_c_even_when_approved_and_clean(self):
        pr = _pr(1, labels=[{"name": SECURITY_LABEL}])
        with patch("router.merge_queue._sam_approved_at_head", new=AsyncMock(return_value=True)):
            bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert (bucket, reason) == ("C", "security_manual")

    async def test_dirty_mergeable_state_is_bucket_c_conflict(self):
        pr = _pr(1, mergeable_state="dirty")
        bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert (bucket, reason) == ("C", "conflict")

    async def test_not_reviewed_at_head_is_bucket_c_unreviewed(self):
        pr = _pr(1, mergeable_state="clean")
        with patch("router.merge_queue._sam_approved_at_head", new=AsyncMock(return_value=False)):
            bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert (bucket, reason) == ("C", "unreviewed")

    async def test_reviewed_but_behind_is_bucket_b(self):
        pr = _pr(1, mergeable_state="behind")
        with patch("router.merge_queue._sam_approved_at_head", new=AsyncMock(return_value=True)):
            bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert (bucket, reason) == ("B", "behind")

    async def test_reviewed_clean_checks_green_is_bucket_a(self):
        pr = _pr(1, mergeable_state="clean")
        with (
            patch("router.merge_queue._sam_approved_at_head", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=True)),
        ):
            bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert (bucket, reason) == ("A", "eligible")

    async def test_reviewed_clean_checks_pending_is_pending_not_digest(self):
        """CI still running must not page the digest — only real blockers do."""
        pr = _pr(1, mergeable_state="clean")
        with (
            patch("router.merge_queue._sam_approved_at_head", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._required_checks_passed", new=AsyncMock(return_value=False)),
        ):
            bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert bucket == "pending"

    async def test_reviewed_unknown_mergeable_state_is_pending(self):
        pr = _pr(1, mergeable_state="unknown")
        with patch("router.merge_queue._sam_approved_at_head", new=AsyncMock(return_value=True)):
            bucket, reason = await _classify_pr("org/repo", pr, "tok")
        assert bucket == "pending"


# ---------------------------------------------------------------------------
# _continuous_tick — bucket partition / no head-of-line blocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestContinuousTick:
    async def test_continuous_merge_skips_blocked_head_of_queue(self, slack_client):
        """Named regression (#832 acceptance criteria):
        test_continuous_merge_skips_blocked_head_of_queue.

        A SECURITY-manual-labeled PR ordered first (bucket C) must not block
        a downstream eligible PR (bucket A) from merging in the same tick.
        """
        blocked = _pr(1, labels=[{"name": SECURITY_LABEL}])
        eligible = _pr(2)

        async def fake_classify(repo, pr, pat):
            if pr["number"] == 1:
                return "C", "security_manual"
            return "A", "eligible"

        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[blocked, eligible])),
            patch(
                "router.merge_queue._get_pr_details",
                new=AsyncMock(side_effect=lambda repo, num, pat: blocked if num == 1 else eligible),
            ),
            patch("router.merge_queue._classify_pr", new=AsyncMock(side_effect=fake_classify)),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)) as mock_merge,
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )

        assert result["merged"] == [2]
        assert result["digest"] == [1]
        mock_merge.assert_awaited_once()
        assert mock_merge.await_args.args[1] == 2

    async def test_multiple_eligible_prs_merge_in_same_tick(self, slack_client):
        """No file-count / one-per-tick cap in continuous mode."""
        pr1, pr2 = _pr(1), _pr(2)
        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr1, pr2])),
            patch(
                "router.merge_queue._get_pr_details",
                new=AsyncMock(side_effect=lambda repo, num, pat: pr1 if num == 1 else pr2),
            ),
            patch("router.merge_queue._classify_pr", new=AsyncMock(return_value=("A", "eligible"))),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        assert sorted(result["merged"]) == [1, 2]

    async def test_bucket_b_updates_branch(self, slack_client):
        pr = _pr(1, mergeable_state="behind")
        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr)),
            patch("router.merge_queue._classify_pr", new=AsyncMock(return_value=("B", "behind"))),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)) as mock_update,
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        assert result["rebased"] == [1]
        mock_update.assert_awaited_once()

    async def test_branch_update_cap_defers_extra_prs(self, slack_client):
        """Rebase-storm mitigation: at most MAX_BRANCH_UPDATES_PER_TICK per tick."""
        prs = [_pr(i, mergeable_state="behind") for i in range(1, MAX_BRANCH_UPDATES_PER_TICK + 3)]
        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=prs)),
            patch(
                "router.merge_queue._get_pr_details",
                new=AsyncMock(side_effect=lambda repo, num, pat: next(p for p in prs if p["number"] == num)),
            ),
            patch("router.merge_queue._classify_pr", new=AsyncMock(return_value=("B", "behind"))),
            patch("router.merge_queue._update_branch", new=AsyncMock(return_value=True)) as mock_update,
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        assert len(result["rebased"]) == MAX_BRANCH_UPDATES_PER_TICK
        assert mock_update.await_count == MAX_BRANCH_UPDATES_PER_TICK

    async def test_bucket_c_posts_one_consolidated_digest(self, slack_client):
        pr1 = _pr(1, mergeable_state="dirty")
        pr2 = _pr(2, labels=[{"name": SECURITY_LABEL}])

        async def fake_classify(repo, pr, pat):
            if pr["number"] == 1:
                return "C", "conflict"
            return "C", "security_manual"

        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr1, pr2])),
            patch(
                "router.merge_queue._get_pr_details",
                new=AsyncMock(side_effect=lambda repo, num, pat: pr1 if num == 1 else pr2),
            ),
            patch("router.merge_queue._classify_pr", new=AsyncMock(side_effect=fake_classify)),
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )

        assert sorted(result["digest"]) == [1, 2]
        slack_client.chat_postMessage.assert_awaited_once()
        text = slack_client.chat_postMessage.call_args.kwargs["text"]
        assert "#1" in text and "#2" in text

    async def test_no_digest_post_when_bucket_c_empty(self, slack_client):
        pr = _pr(1)
        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr)),
            patch("router.merge_queue._classify_pr", new=AsyncMock(return_value=("A", "eligible"))),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.merge_queue._verify_merged", new=AsyncMock(return_value=True)),
        ):
            await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_pending_pr_is_neither_merged_nor_digested(self, slack_client):
        pr = _pr(1)
        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr)),
            patch("router.merge_queue._classify_pr", new=AsyncMock(return_value=("pending", "checks_pending"))),
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        assert result["merged"] == []
        assert result["digest"] == []
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_dry_run_does_not_merge_rebase_or_post(self, slack_client):
        merge_pr = _pr(1)
        rebase_pr = _pr(2, mergeable_state="behind")
        digest_pr = _pr(3, mergeable_state="dirty")

        async def fake_classify(repo, pr, pat):
            return {1: ("A", "eligible"), 2: ("B", "behind"), 3: ("C", "conflict")}[pr["number"]]

        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[merge_pr, rebase_pr, digest_pr])),
            patch(
                "router.merge_queue._get_pr_details",
                new=AsyncMock(side_effect=lambda repo, num, pat: {1: merge_pr, 2: rebase_pr, 3: digest_pr}[num]),
            ),
            patch("router.merge_queue._classify_pr", new=AsyncMock(side_effect=fake_classify)),
            patch("router.merge_queue._squash_merge", new=AsyncMock()) as mock_merge,
            patch("router.merge_queue._update_branch", new=AsyncMock()) as mock_update,
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=True
            )

        mock_merge.assert_not_awaited()
        mock_update.assert_not_awaited()
        slack_client.chat_postMessage.assert_not_awaited()
        # Dry-run still reports what it *would* have done.
        assert result["merged"] == [1]
        assert result["rebased"] == [2]
        assert result["digest"] == [3]

    async def test_head_moved_409_is_not_double_merged(self, slack_client):
        """Idempotency: a 409 (HEAD moved) drops the PR without a duplicate merge attempt."""
        pr = _pr(1)
        with (
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[pr])),
            patch("router.merge_queue._get_pr_details", new=AsyncMock(return_value=pr)),
            patch("router.merge_queue._classify_pr", new=AsyncMock(return_value=("A", "eligible"))),
            patch("router.merge_queue._squash_merge", new=AsyncMock(return_value=None)) as mock_merge,
            patch("router.merge_queue._verify_merged", new=AsyncMock()) as mock_verify,
        ):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        assert result["merged"] == []
        mock_merge.assert_awaited_once()
        mock_verify.assert_not_awaited()

    async def test_token_error_aborts_tick_and_posts_slack(self, slack_client):
        with patch("router.merge_queue._get_open_prs", new=AsyncMock(side_effect=TokenError("bad token"))):
            result = await _continuous_tick(
                repo="org/repo", pat="tok", slack_client=slack_client, destination="C_NOTIFY", dry_run=False
            )
        assert result["skipped"] == "token_error"
        slack_client.chat_postMessage.assert_awaited_once()


# ---------------------------------------------------------------------------
# tick() — CONTINUOUS_MERGE flag wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTickContinuousMergeFlag:
    async def test_flag_off_uses_legacy_path(self, tmp_path, slack_client, now):
        """Default-off: tick() must not call _continuous_tick when the flag is unset."""
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.merge_queue._get_open_prs", new=AsyncMock(return_value=[])),
            patch("router.merge_queue._continuous_tick", new=AsyncMock()) as mock_continuous,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        mock_continuous.assert_not_awaited()
        assert result["skipped"] == "no_open_prs"

    async def test_flag_on_delegates_to_continuous_tick(self, tmp_path, slack_client, now):
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.settings.get", side_effect=lambda k: {"CONTINUOUS_MERGE": True}.get(k, False)),
            patch(
                "router.merge_queue._continuous_tick",
                new=AsyncMock(return_value={"status": "ok", "action": "continuous"}),
            ) as mock_continuous,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        mock_continuous.assert_awaited_once()
        assert result["action"] == "continuous"

    async def test_flag_on_passes_dry_run_default_true(self, tmp_path, slack_client, now):
        """CONTINUOUS_MERGE_DRY_RUN defaults True — shadow mode on first flip."""
        payload = _make_payload(tmp_path)
        with (
            patch("router.merge_queue.is_system_idle", return_value=(True, None)),
            patch("router.settings.get", side_effect=lambda k: {"CONTINUOUS_MERGE": True}.get(k, True)),
            patch(
                "router.merge_queue._continuous_tick",
                new=AsyncMock(return_value={"status": "ok"}),
            ) as mock_continuous,
        ):
            await tick(payload=payload, slack_client=slack_client, now=now)
        assert mock_continuous.await_args.kwargs["dry_run"] is True
