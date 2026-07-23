"""Unit tests for ``router.github_api.gh_get_all`` — the paginating list
helper added to stop unpaginated GitHub list call-sites from silently
capping results at page 1 (#790).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from router.github_api import DEFAULT_TIMEOUT_SECONDS, TokenError, auth_headers, gh_get_all

pytestmark = pytest.mark.unit


def _mock_async_client() -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    client_cls = MagicMock()
    client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
    client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return client_cls, client


def _page(json_data, link_header: str | None = None, status_code: int = 200) -> httpx.Response:
    headers = {"Link": link_header} if link_header else {}
    request = httpx.Request("GET", "https://api.github.com/repos/o/r/pulls")
    return httpx.Response(status_code, json=json_data, headers=headers, request=request)


class TestGhGetAllPagination:
    @pytest.mark.asyncio
    async def test_gh_get_all_follows_link_next(self):
        page1 = [{"number": 1}, {"number": 2}]
        page2 = [{"number": 3}]
        next_url = "https://api.github.com/repos/o/r/pulls?per_page=100&page=2&state=open"
        client_cls, client = _mock_async_client()
        client.get = AsyncMock(side_effect=[_page(page1, f'<{next_url}>; rel="next"'), _page(page2)])

        with patch("router.github_api.httpx.AsyncClient", client_cls):
            result = await gh_get_all("/repos/o/r/pulls", "ghp_abc", state="open")

        assert result == page1 + page2
        client_cls.assert_called_once_with(timeout=DEFAULT_TIMEOUT_SECONDS)
        assert client.get.await_count == 2
        first_call, second_call = client.get.await_args_list
        assert first_call.args == ("https://api.github.com/repos/o/r/pulls",)
        assert first_call.kwargs == {
            "headers": auth_headers("ghp_abc"),
            "params": {"state": "open", "per_page": 100},
        }
        # The Link: rel="next" URL already carries its own query string.
        assert second_call.args == (next_url,)
        assert second_call.kwargs == {"headers": auth_headers("ghp_abc"), "params": None}

    @pytest.mark.asyncio
    async def test_single_page_no_next_link_issues_one_request(self):
        client_cls, client = _mock_async_client()
        client.get = AsyncMock(return_value=_page([{"number": 1}]))

        with patch("router.github_api.httpx.AsyncClient", client_cls):
            result = await gh_get_all("/repos/o/r/pulls", "ghp_abc")

        assert result == [{"number": 1}]
        client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_401_raises_token_error(self):
        client_cls, client = _mock_async_client()
        client.get = AsyncMock(return_value=_page({}, status_code=401))

        with patch("router.github_api.httpx.AsyncClient", client_cls):
            with pytest.raises(TokenError):
                await gh_get_all("/repos/o/r/pulls", "ghp_abc")

    @pytest.mark.asyncio
    async def test_non_2xx_raises_http_status_error(self):
        client_cls, client = _mock_async_client()
        client.get = AsyncMock(return_value=_page({}, status_code=500))

        with patch("router.github_api.httpx.AsyncClient", client_cls):
            with pytest.raises(httpx.HTTPStatusError):
                await gh_get_all("/repos/o/r/pulls", "ghp_abc")

    @pytest.mark.asyncio
    async def test_max_pages_cap_stops_and_logs(self, caplog):
        next_url = "https://api.github.com/repos/o/r/pulls?page=2"
        client_cls, client = _mock_async_client()
        # Every page (including the last one returned) still advertises a next link.
        client.get = AsyncMock(return_value=_page([{"number": 1}], f'<{next_url}>; rel="next"'))

        with patch("router.github_api.httpx.AsyncClient", client_cls), caplog.at_level("WARNING"):
            result = await gh_get_all("/repos/o/r/pulls", "ghp_abc", max_pages=2)

        assert result == [{"number": 1}, {"number": 1}]
        assert client.get.await_count == 2
        assert any("max_pages" in r.message for r in caplog.records)
