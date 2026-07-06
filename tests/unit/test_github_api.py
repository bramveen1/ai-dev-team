"""Unit tests for router.github_api — shared GitHub REST helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import auto_dispatch, github_api, merge_queue
from router.github_api import (
    DEFAULT_TIMEOUT_SECONDS,
    TokenError,
    auth_headers,
    gh_get,
    gh_post,
    gh_put,
    read_pat,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# read_pat
# ---------------------------------------------------------------------------


class TestReadPat:
    def test_reads_valid_token(self, tmp_path):
        pat_file = tmp_path / "token"
        pat_file.write_text("ghp_validtoken123\n")
        assert read_pat(str(pat_file)) == "ghp_validtoken123"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(TokenError, match="not found"):
            read_pat(str(tmp_path / "nonexistent"))

    def test_raises_on_empty_file(self, tmp_path):
        pat_file = tmp_path / "token"
        pat_file.write_text("   \n")
        with pytest.raises(TokenError, match="empty"):
            read_pat(str(pat_file))


# ---------------------------------------------------------------------------
# auth_headers
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    def test_bearer_and_api_version(self):
        headers = auth_headers("ghp_abc")
        assert headers["Authorization"] == "Bearer ghp_abc"
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# gh_get / gh_put / gh_post
# ---------------------------------------------------------------------------


def _mock_async_client() -> tuple[MagicMock, MagicMock]:
    """Return (client_cls, client) where client_cls stands in for httpx.AsyncClient."""
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200))
    client.put = AsyncMock(return_value=MagicMock(status_code=200))
    client.post = AsyncMock(return_value=MagicMock(status_code=200))
    client_cls = MagicMock()
    client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
    client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return client_cls, client


class TestGhWrappers:
    @pytest.mark.asyncio
    async def test_gh_get_builds_url_headers_and_timeout(self):
        client_cls, client = _mock_async_client()
        with patch("router.github_api.httpx.AsyncClient", client_cls):
            await gh_get("/repos/o/r/pulls", "ghp_abc", state="open")
        client_cls.assert_called_once_with(timeout=DEFAULT_TIMEOUT_SECONDS)
        client.get.assert_awaited_once_with(
            "https://api.github.com/repos/o/r/pulls",
            headers=auth_headers("ghp_abc"),
            params={"state": "open"},
        )

    @pytest.mark.asyncio
    async def test_gh_put_defaults_to_empty_body(self):
        client_cls, client = _mock_async_client()
        with patch("router.github_api.httpx.AsyncClient", client_cls):
            await gh_put("/repos/o/r/pulls/1/merge", "ghp_abc")
        client_cls.assert_called_once_with(timeout=DEFAULT_TIMEOUT_SECONDS)
        client.put.assert_awaited_once_with(
            "https://api.github.com/repos/o/r/pulls/1/merge",
            headers=auth_headers("ghp_abc"),
            json={},
        )

    @pytest.mark.asyncio
    async def test_gh_post_sends_body(self):
        client_cls, client = _mock_async_client()
        with patch("router.github_api.httpx.AsyncClient", client_cls):
            await gh_post("/repos/o/r/issues", "ghp_abc", {"title": "t"})
        client.post.assert_awaited_once_with(
            "https://api.github.com/repos/o/r/issues",
            headers=auth_headers("ghp_abc"),
            json={"title": "t"},
        )


# ---------------------------------------------------------------------------
# Consumer aliases — auto_dispatch and merge_queue must share one helper layer
# ---------------------------------------------------------------------------


class TestConsumerAliases:
    def test_token_error_is_one_class(self):
        assert merge_queue.TokenError is github_api.TokenError
        assert auto_dispatch._TokenError is github_api.TokenError

    def test_pat_paths_agree(self):
        assert merge_queue.MERGE_PAT_PATH == github_api.MERGE_PAT_PATH
        assert auto_dispatch.MERGE_PAT_PATH == github_api.MERGE_PAT_PATH
