"""Tests for helpers/auth.py — login, CSRF fetch, and session refresh."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers.auth import AuthError, RateLimitError, fetch_csrf, login, refresh_session

pytestmark = pytest.mark.unit

BASE_URL = "https://api.pathtohired.com"


def _make_response(status: int, headers: dict | None = None, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json = MagicMock(return_value=json_body or {})
    return resp


def _make_client(*responses: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(responses))
    client.post = AsyncMock(side_effect=list(responses))
    return client


class TestLogin:
    @pytest.mark.asyncio
    async def test_returns_csrf_from_login_response(self) -> None:
        resp = _make_response(200, headers={"X-CSRF-Token": "tok123"})
        client = _make_client(resp)
        token = await login(client, BASE_URL, "u", "p")
        assert token == "tok123"
        client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_fetch_csrf_when_header_absent(self) -> None:
        login_resp = _make_response(200, headers={})
        csrf_resp = _make_response(200, headers={"X-CSRF-Token": "from-csrf"})
        client = MagicMock()
        client.post = AsyncMock(return_value=login_resp)
        client.get = AsyncMock(return_value=csrf_resp)
        token = await login(client, BASE_URL, "u", "p")
        assert token == "from-csrf"
        client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit_error(self) -> None:
        resp = _make_response(429)
        client = _make_client(resp)
        with pytest.raises(RateLimitError, match="rate-limited"):
            await login(client, BASE_URL, "u", "p")

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self) -> None:
        resp = _make_response(401)
        client = _make_client(resp)
        with pytest.raises(AuthError, match="Login failed"):
            await login(client, BASE_URL, "u", "p")

    @pytest.mark.asyncio
    async def test_500_raises_auth_error(self) -> None:
        resp = _make_response(500)
        client = _make_client(resp)
        with pytest.raises(AuthError, match="Login failed"):
            await login(client, BASE_URL, "u", "p")

    @pytest.mark.asyncio
    async def test_204_accepted(self) -> None:
        resp = _make_response(204, headers={"X-CSRF-Token": "t"})
        client = _make_client(resp)
        token = await login(client, BASE_URL, "u", "p")
        assert token == "t"


class TestFetchCsrf:
    @pytest.mark.asyncio
    async def test_returns_token_from_header(self) -> None:
        resp = _make_response(200, headers={"X-CSRF-Token": "fresh"})
        client = _make_client(resp)
        token = await fetch_csrf(client, BASE_URL)
        assert token == "fresh"

    @pytest.mark.asyncio
    async def test_non_2xx_raises(self) -> None:
        resp = _make_response(503)
        client = _make_client(resp)
        with pytest.raises(AuthError, match="CSRF fetch failed"):
            await fetch_csrf(client, BASE_URL)

    @pytest.mark.asyncio
    async def test_missing_token_in_header_raises(self) -> None:
        resp = _make_response(200, headers={})
        client = _make_client(resp)
        with pytest.raises(AuthError, match="CSRF token not found"):
            await fetch_csrf(client, BASE_URL)


class TestRefreshSession:
    @pytest.mark.asyncio
    async def test_posts_refresh_then_fetches_csrf(self) -> None:
        refresh_resp = _make_response(200, headers={})
        csrf_resp = _make_response(200, headers={"X-CSRF-Token": "new-tok"})
        client = MagicMock()
        client.post = AsyncMock(return_value=refresh_resp)
        client.get = AsyncMock(return_value=csrf_resp)
        token = await refresh_session(client, BASE_URL)
        assert token == "new-tok"
        client.post.assert_called_once_with(f"{BASE_URL}/api/auth/refresh")
        client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_failure_raises(self) -> None:
        resp = _make_response(401)
        client = _make_client(resp)
        with pytest.raises(AuthError, match="Session refresh failed"):
            await refresh_session(client, BASE_URL)
