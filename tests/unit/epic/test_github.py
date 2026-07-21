"""Unit tests for router.epic.github — issue/PR reads + label write (#755)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.epic.github import (
    TokenError,
    _apply_epic_label,
    _get_issue,
    _get_open_pr_for_issue,
    _has_merged_closing_pr,
    _is_child_terminal,
)

pytestmark = pytest.mark.unit


def _resp(status_code, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.raise_for_status = MagicMock()
    return r


@pytest.mark.asyncio
class TestGetIssue:
    async def test_returns_payload_on_200(self):
        payload = {"number": 101, "title": "A sub-issue", "html_url": "https://x/101"}
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(200, payload))):
            issue = await _get_issue("o/r", 101, "pat")
        assert issue == payload

    async def test_non_200_returns_none(self):
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(404))):
            issue = await _get_issue("o/r", 101, "pat")
        assert issue is None

    async def test_401_raises_token_error(self):
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(401))):
            with pytest.raises(TokenError):
                await _get_issue("o/r", 101, "pat")


@pytest.mark.asyncio
class TestGetOpenPrForIssue:
    async def test_matches_closes_reference(self):
        prs = [{"number": 5, "title": "Fixes #101", "body": ""}]
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(200, prs))):
            pr = await _get_open_pr_for_issue("o/r", 101, "pat")
        assert pr["number"] == 5

    async def test_no_match_returns_none(self):
        prs = [{"number": 5, "title": "unrelated change", "body": ""}]
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(200, prs))):
            pr = await _get_open_pr_for_issue("o/r", 101, "pat")
        assert pr is None

    async def test_401_raises_token_error(self):
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(401))):
            with pytest.raises(TokenError):
                await _get_open_pr_for_issue("o/r", 101, "pat")


@pytest.mark.asyncio
class TestHasMergedClosingPr:
    async def test_merged_matching_pr_is_true(self):
        prs = [{"number": 5, "title": "Fixes #101", "body": "", "merged_at": "2026-07-20T00:00:00Z"}]
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(200, prs))):
            assert await _has_merged_closing_pr("o/r", 101, "pat") is True

    async def test_closed_but_unmerged_matching_pr_is_false(self):
        prs = [{"number": 5, "title": "Fixes #101", "body": "", "merged_at": None}]
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(200, prs))):
            assert await _has_merged_closing_pr("o/r", 101, "pat") is False

    async def test_no_matching_pr_is_false(self):
        prs = [{"number": 5, "title": "unrelated", "body": "", "merged_at": "2026-07-20T00:00:00Z"}]
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(200, prs))):
            assert await _has_merged_closing_pr("o/r", 101, "pat") is False

    async def test_401_raises_token_error(self):
        with patch("router.epic.github._gh_get", new=AsyncMock(return_value=_resp(401))):
            with pytest.raises(TokenError):
                await _has_merged_closing_pr("o/r", 101, "pat")


@pytest.mark.asyncio
class TestIsChildTerminal:
    async def test_closed_issue_is_terminal(self):
        issue = {"number": 101, "state": "closed"}
        with (
            patch("router.epic.github._get_issue", new=AsyncMock(return_value=issue)),
            patch("router.epic.github._has_merged_closing_pr", new=AsyncMock(return_value=False)),
        ):
            assert await _is_child_terminal("o/r", 101, "pat") is True

    async def test_merged_closing_pr_is_terminal_even_if_issue_open(self):
        issue = {"number": 101, "state": "open"}
        with (
            patch("router.epic.github._get_issue", new=AsyncMock(return_value=issue)),
            patch("router.epic.github._has_merged_closing_pr", new=AsyncMock(return_value=True)),
        ):
            assert await _is_child_terminal("o/r", 101, "pat") is True

    async def test_open_issue_no_merged_pr_is_not_terminal(self):
        issue = {"number": 101, "state": "open"}
        with (
            patch("router.epic.github._get_issue", new=AsyncMock(return_value=issue)),
            patch("router.epic.github._has_merged_closing_pr", new=AsyncMock(return_value=False)),
        ):
            assert await _is_child_terminal("o/r", 101, "pat") is False

    async def test_unreadable_issue_falls_back_to_merged_pr_check(self):
        with (
            patch("router.epic.github._get_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.github._has_merged_closing_pr", new=AsyncMock(return_value=True)),
        ):
            assert await _is_child_terminal("o/r", 101, "pat") is True


@pytest.mark.asyncio
class TestApplyEpicLabel:
    async def test_posts_label(self):
        post = AsyncMock(return_value=_resp(200))
        with patch("router.epic.github._gh_post", new=post):
            ok = await _apply_epic_label("o/r", 9, "epic:foo", "pat")
        assert ok is True
        assert post.await_args.args[0] == "/repos/o/r/issues/9/labels"
        assert post.await_args.args[2] == {"labels": ["epic:foo"]}

    async def test_201_created_is_success(self):
        with patch("router.epic.github._gh_post", new=AsyncMock(return_value=_resp(201))):
            ok = await _apply_epic_label("o/r", 9, "epic:foo", "pat")
        assert ok is True

    async def test_non_2xx_returns_false(self):
        with patch("router.epic.github._gh_post", new=AsyncMock(return_value=_resp(422))):
            ok = await _apply_epic_label("o/r", 9, "epic:foo", "pat")
        assert ok is False

    async def test_401_raises_token_error(self):
        with patch("router.epic.github._gh_post", new=AsyncMock(return_value=_resp(401))):
            with pytest.raises(TokenError):
                await _apply_epic_label("o/r", 9, "epic:foo", "pat")
