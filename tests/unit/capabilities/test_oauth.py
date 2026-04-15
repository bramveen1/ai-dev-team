"""Tests for the OAuth2 device code flow and token refresh utilities.

All HTTP calls are mocked — no real OAuth endpoints are contacted.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from capabilities.oauth import (
    OAuthError,
    ensure_valid_token,
    refresh_access_token,
    start_device_code_flow,
)
from capabilities.secrets import SecretStore

pytestmark = pytest.mark.unit


class MockResponse:
    """Mock httpx response."""

    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def store(tmp_path):
    """Create a SecretStore backed by a temp directory."""
    d = tmp_path / "secrets"
    d.mkdir()
    return SecretStore(d)


def _write_secrets(store, provider, data):
    store.save(provider, data)


class TestStartDeviceCodeFlow:
    @pytest.mark.asyncio
    async def test_success(self):
        expected = {
            "user_code": "L9J8LQCM7",
            "verification_uri": "https://microsoft.com/devicelogin",
            "device_code": "device123",
            "expires_in": 900,
            "interval": 5,
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=MockResponse(200, expected))

        with patch("capabilities.oauth.httpx.AsyncClient", return_value=mock_client):
            result = await start_device_code_flow(
                tenant_id="tenant123",
                client_id="client456",
                scopes="Mail.Read.Shared",
                client_secret="secret789",
            )

        assert result["user_code"] == "L9J8LQCM7"
        assert result["device_code"] == "device123"

        # Verify the POST was called with correct data
        call_kwargs = mock_client.post.call_args
        assert "tenant123" in call_kwargs.args[0]
        post_data = call_kwargs.kwargs.get("data", call_kwargs.args[1] if len(call_kwargs.args) > 1 else {})
        assert post_data["client_id"] == "client456"
        assert post_data["client_secret"] == "secret789"

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=MockResponse(400, {"error": "bad_request"}))

        with patch("capabilities.oauth.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(OAuthError, match="400"):
                await start_device_code_flow("t", "c", "s")

    @pytest.mark.asyncio
    async def test_no_client_secret(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=MockResponse(200, {"device_code": "d"}))

        with patch("capabilities.oauth.httpx.AsyncClient", return_value=mock_client):
            await start_device_code_flow("t", "c", "s")

        post_data = mock_client.post.call_args.kwargs.get(
            "data", mock_client.post.call_args.args[1] if len(mock_client.post.call_args.args) > 1 else {}
        )
        assert "client_secret" not in post_data


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_success(self):
        token_response = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=MockResponse(200, token_response))

        with patch("capabilities.oauth.httpx.AsyncClient", return_value=mock_client):
            result = await refresh_access_token(
                tenant_id="tenant123",
                client_id="client456",
                refresh_token="old_refresh",
                scopes="Mail.Read.Shared",
                client_secret="secret",
            )

        assert result["access_token"] == "new_access"
        assert result["refresh_token"] == "new_refresh"

    @pytest.mark.asyncio
    async def test_expired_refresh_raises(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(
            return_value=MockResponse(400, {"error": "invalid_grant", "error_description": "Token expired"})
        )

        with patch("capabilities.oauth.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(OAuthError, match="refresh failed"):
                await refresh_access_token("t", "c", "old", "s")


class TestEnsureValidToken:
    @pytest.mark.asyncio
    async def test_returns_cached_when_fresh(self, store):
        _write_secrets(
            store,
            "m365",
            {
                "access_token": "fresh_tok",
                "expires_at": int(time.time()) + 3600,
            },
        )
        oauth_config = {"authority": "https://login.microsoftonline.com", "token_path": "/oauth2/v2.0/token"}
        result = await ensure_valid_token(store, "m365", oauth_config)
        assert result == "fresh_tok"

    @pytest.mark.asyncio
    async def test_refreshes_when_expired(self, store):
        _write_secrets(
            store,
            "m365",
            {
                "access_token": "old_tok",
                "refresh_token": "refresh_tok",
                "expires_at": int(time.time()) - 100,
                "tenant_id": "tenant123",
                "client_id": "client456",
                "client_secret": "secret",
                "scopes": "Mail.Read.Shared",
            },
        )

        token_response = {
            "access_token": "new_tok",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=MockResponse(200, token_response))

        with patch("capabilities.oauth.httpx.AsyncClient", return_value=mock_client):
            oauth_config = {"authority": "https://login.microsoftonline.com", "token_path": "/oauth2/v2.0/token"}
            result = await ensure_valid_token(store, "m365", oauth_config)

        assert result == "new_tok"

        # Verify tokens were saved
        store.invalidate("m365")
        saved = store.load("m365")
        assert saved["access_token"] == "new_tok"
        assert saved["refresh_token"] == "new_refresh"
        assert saved["expires_at"] > int(time.time())

    @pytest.mark.asyncio
    async def test_missing_credentials_raises(self, store):
        _write_secrets(
            store,
            "m365",
            {
                "access_token": "old",
                "expires_at": int(time.time()) - 100,
                # Missing tenant_id, client_id, refresh_token
            },
        )
        oauth_config = {"authority": "https://login.microsoftonline.com", "token_path": "/oauth2/v2.0/token"}
        with pytest.raises(OAuthError, match="missing"):
            await ensure_valid_token(store, "m365", oauth_config)
