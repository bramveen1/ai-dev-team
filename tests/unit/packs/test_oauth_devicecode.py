"""Unit tests for router.packs.oauth_devicecode.

Mocks httpx so no real OAuth provider is contacted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.packs.oauth_devicecode import (
    OAuthError,
    poll_for_token,
    start_device_code,
)

pytestmark = pytest.mark.unit


def _mock_response(status_code: int, payload: dict | None = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.text = text or (str(payload) if payload else "")
    return response


@pytest.fixture()
def mock_httpx_post():
    """Patch httpx.AsyncClient with a controllable AsyncMock for ``post``."""
    with patch("router.packs.oauth_devicecode.httpx.AsyncClient") as client_cls:
        ctx = MagicMock()
        post = AsyncMock()
        ctx.post = post
        client_cls.return_value.__aenter__.return_value = ctx
        client_cls.return_value.__aexit__.return_value = None
        yield post


class TestStartDeviceCode:
    @pytest.mark.asyncio
    async def test_returns_provider_response(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(
            200,
            {"user_code": "ABCD-1234", "verification_uri": "https://example/code", "device_code": "dc"},
        )
        result = await start_device_code(
            device_code_url="https://example/code",
            client_id="cid",
            scope="repo",
        )
        assert result["user_code"] == "ABCD-1234"

    @pytest.mark.asyncio
    async def test_non_200_raises(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(400, text="Bad Request")
        with pytest.raises(OAuthError, match="device-code request failed"):
            await start_device_code(
                device_code_url="https://example/code",
                client_id="cid",
                scope="repo",
            )

    @pytest.mark.asyncio
    async def test_extra_params_included(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(200, {"user_code": "x"})
        await start_device_code(
            device_code_url="https://example/code",
            client_id="cid",
            scope="repo",
            extra_params={"client_secret": "shh"},
        )
        kwargs = mock_httpx_post.call_args.kwargs
        assert kwargs["data"]["client_secret"] == "shh"


class TestPollForToken:
    @pytest.mark.asyncio
    async def test_success_first_poll(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(
            200,
            {"access_token": "abc", "refresh_token": "ref"},
        )
        result = await poll_for_token(
            token_url="https://example/token",
            client_id="cid",
            device_code="dc",
        )
        assert result["access_token"] == "abc"

    @pytest.mark.asyncio
    async def test_pending_then_success(self, mock_httpx_post) -> None:
        mock_httpx_post.side_effect = [
            _mock_response(400, {"error": "authorization_pending"}),
            _mock_response(200, {"access_token": "abc"}),
        ]
        with patch("router.packs.oauth_devicecode.asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="dc",
                interval=1,
            )
        assert result["access_token"] == "abc"

    @pytest.mark.asyncio
    async def test_user_declines_raises(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(400, {"error": "access_denied"})
        with pytest.raises(OAuthError, match="declined"):
            await poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="dc",
            )

    @pytest.mark.asyncio
    async def test_expired_token_raises(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(400, {"error": "expired_token"})
        with pytest.raises(OAuthError, match="expired"):
            await poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="dc",
            )

    @pytest.mark.asyncio
    async def test_unknown_error_raises(self, mock_httpx_post) -> None:
        mock_httpx_post.return_value = _mock_response(
            400,
            {"error": "invalid_request", "error_description": "no idea"},
        )
        with pytest.raises(OAuthError, match="invalid_request"):
            await poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="dc",
            )

    @pytest.mark.asyncio
    async def test_slow_down_increases_interval(self, mock_httpx_post) -> None:
        mock_httpx_post.side_effect = [
            _mock_response(400, {"error": "slow_down"}),
            _mock_response(200, {"access_token": "abc"}),
        ]
        with patch("router.packs.oauth_devicecode.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await poll_for_token(
                token_url="https://example/token",
                client_id="cid",
                device_code="dc",
                interval=2,
            )
        # First sleep should be 2 + 5 = 7s (slow_down adds 5).
        assert sleep.call_args_list[0].args[0] == 7
