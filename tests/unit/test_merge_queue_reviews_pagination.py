"""Regression tests for #787 — `_has_approving_review` must paginate the full
review history, not just the first page (GitHub's default `per_page` is 30).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.merge_queue import _has_approving_review

pytestmark = pytest.mark.unit


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


def _page_response(reviews: list[dict]) -> MagicMock:
    return MagicMock(
        status_code=200,
        raise_for_status=MagicMock(),
        json=MagicMock(return_value=reviews),
    )


class TestHasApprovingReviewPagination:
    @pytest.mark.asyncio
    async def test_late_changes_requested_on_page_two_blocks_merge(self, sample_pr, monkeypatch):
        """sam APPROVED on page 1, then CHANGES_REQUESTED on page 2 → blocked."""
        page1 = [{"state": "APPROVED", "user": {"login": "sam"}}] + [
            {"state": "COMMENTED", "user": {"login": f"bot-{i}"}} for i in range(99)
        ]
        page2 = [{"state": "CHANGES_REQUESTED", "user": {"login": "sam"}}]
        responses = [_page_response(page1), _page_response(page2)]

        monkeypatch.setattr("router.merge_queue._gh_get", AsyncMock(side_effect=responses))
        result = await _has_approving_review("org/repo", 10, sample_pr, "tok")

        assert result is False

    @pytest.mark.asyncio
    async def test_late_approval_on_page_two_is_honoured(self, sample_pr, monkeypatch):
        """sam CHANGES_REQUESTED on page 1, then APPROVED on page 2 → honoured."""
        page1 = [{"state": "CHANGES_REQUESTED", "user": {"login": "sam"}}] + [
            {"state": "COMMENTED", "user": {"login": f"bot-{i}"}} for i in range(99)
        ]
        page2 = [{"state": "APPROVED", "user": {"login": "sam"}}]
        responses = [_page_response(page1), _page_response(page2)]

        monkeypatch.setattr("router.merge_queue._gh_get", AsyncMock(side_effect=responses))
        result = await _has_approving_review("org/repo", 10, sample_pr, "tok")

        assert result is True

    @pytest.mark.asyncio
    async def test_no_pagination_when_single_page(self, sample_pr, monkeypatch):
        """PRs with <=30 reviews (a single, non-full page) behave exactly as before."""
        reviews = [{"state": "APPROVED", "user": {"login": "bob"}}]
        mock_get = AsyncMock(return_value=_page_response(reviews))

        monkeypatch.setattr("router.merge_queue._gh_get", mock_get)
        result = await _has_approving_review("org/repo", 10, sample_pr, "tok")

        assert result is True
        mock_get.assert_awaited_once()
