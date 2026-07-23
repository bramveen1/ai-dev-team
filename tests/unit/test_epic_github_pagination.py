"""Regression test for #790 — ``_has_merged_closing_pr`` must see closed PRs
beyond page 1, not just the first ``per_page=100`` page. A merged closing PR
that has fallen off page 1 previously read as "not merged", which made
``_is_child_terminal`` return False and re-triggered dispatch/approval for
already-shipped epic work on every tick.

Exercises the real pagination path end to end (``_has_merged_closing_pr`` ->
``gh_get_all`` -> ``Link: rel="next"``-following HTTP client), rather than
stubbing ``_gh_get_all`` itself, so it actually proves page 2 gets fetched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from router.epic.github import _has_merged_closing_pr

pytestmark = pytest.mark.unit


def _page(json_data, link_header: str | None = None) -> httpx.Response:
    headers = {"Link": link_header} if link_header else {}
    request = httpx.Request("GET", "https://api.github.com/repos/o/r/pulls")
    return httpx.Response(200, json=json_data, headers=headers, request=request)


@pytest.mark.asyncio
async def test_merged_closing_pr_found_on_page_two():
    # Page 1 is a full page of unrelated closed PRs; the merged closing PR is
    # the sole entry on page 2, only reachable by following Link: rel="next".
    page1 = [{"number": i, "title": "unrelated", "body": "", "merged_at": None} for i in range(100)]
    page2 = [{"number": 999, "title": "Fixes #101", "body": "", "merged_at": "2026-07-20T00:00:00Z"}]
    next_url = "https://api.github.com/repos/o/r/pulls?state=closed&per_page=100&page=2"

    client = MagicMock()
    client.get = AsyncMock(side_effect=[_page(page1, f'<{next_url}>; rel="next"'), _page(page2)])
    client_cls = MagicMock()
    client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
    client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("router.github_api.httpx.AsyncClient", client_cls):
        assert await _has_merged_closing_pr("o/r", 101, "pat") is True

    assert client.get.await_count == 2
