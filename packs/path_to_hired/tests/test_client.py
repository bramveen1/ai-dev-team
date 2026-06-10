"""Tests for helpers/client.py — 401/403 retry logic and CSRF header injection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers.client import PathToHiredClient, _is_csrf_error
from helpers.secrets import Credentials

pytestmark = pytest.mark.unit

_CREDS = Credentials(base_url="https://api.pathtohired.com", username="u", password="p")


def _resp(status: int, headers: dict | None = None, body: dict | None = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json = MagicMock(return_value=body or {})
    r.text = text
    return r


def _patch_login(token: str = "csrf-token"):
    """Patch auth.login so PathToHiredClient._start_session returns a fixed token."""
    return patch("helpers.client.login", new=AsyncMock(return_value=token))


def _patch_refresh(token: str = "refreshed-csrf"):
    return patch("helpers.client.refresh_session", new=AsyncMock(return_value=token))


def _patch_fetch_csrf(token: str = "new-csrf"):
    return patch("helpers.client.fetch_csrf", new=AsyncMock(return_value=token))


class TestIsCsrfError:
    def test_detects_csrf_in_json_message(self) -> None:
        r = _resp(403, body={"message": "CSRF token invalid"})
        assert _is_csrf_error(r)

    def test_detects_csrf_in_json_error_key(self) -> None:
        r = _resp(403, body={"error": "csrf mismatch"})
        assert _is_csrf_error(r)

    def test_detects_csrf_in_text(self) -> None:
        r = _resp(403, text="forbidden: csrf check failed")
        r.json = MagicMock(side_effect=ValueError)
        assert _is_csrf_error(r)

    def test_non_csrf_body_returns_false(self) -> None:
        r = _resp(403, body={"message": "forbidden"})
        assert not _is_csrf_error(r)


class TestPathToHiredClientInit:
    @pytest.mark.asyncio
    async def test_start_session_sets_csrf_token(self) -> None:
        with _patch_login("tok") as mock_login:
            async with PathToHiredClient(_CREDS) as client:
                assert client._csrf_token == "tok"
            mock_login.assert_called_once()


class TestRequestHappyPath:
    @pytest.mark.asyncio
    async def test_get_injects_csrf_header(self) -> None:
        api_resp = _resp(200, body={"users": []})
        with _patch_login("my-csrf"):
            async with PathToHiredClient(_CREDS) as client:
                client._http.request = AsyncMock(return_value=api_resp)
                resp = await client.get("/api/admin/users")
        assert resp.status_code == 200
        call_kwargs = client._http.request.call_args
        assert call_kwargs[1]["headers"]["X-CSRF-Token"] == "my-csrf"

    @pytest.mark.asyncio
    async def test_post_injects_csrf_header(self) -> None:
        api_resp = _resp(200, body={"ok": True})
        with _patch_login("csrf-x"):
            async with PathToHiredClient(_CREDS) as client:
                client._http.request = AsyncMock(return_value=api_resp)
                resp = await client.post("/api/admin/users/1/approve")
        assert resp.status_code == 200


class TestRetryOn401:
    @pytest.mark.asyncio
    async def test_401_triggers_refresh_and_retry(self) -> None:
        first_resp = _resp(401)
        ok_resp = _resp(200, body={"users": []})

        with _patch_login("initial-csrf"):
            with _patch_refresh("refreshed-csrf"):
                async with PathToHiredClient(_CREDS) as client:
                    client._http.request = AsyncMock(side_effect=[first_resp, ok_resp])
                    resp = await client.get("/api/admin/users")

        assert resp.status_code == 200
        assert client._http.request.call_count == 2
        second_call = client._http.request.call_args_list[1]
        assert second_call[1]["headers"]["X-CSRF-Token"] == "refreshed-csrf"

    @pytest.mark.asyncio
    async def test_csrf_token_updated_after_401_refresh(self) -> None:
        first_resp = _resp(401)
        ok_resp = _resp(200)

        with _patch_login("old-csrf"):
            with _patch_refresh("new-csrf"):
                async with PathToHiredClient(_CREDS) as client:
                    client._http.request = AsyncMock(side_effect=[first_resp, ok_resp])
                    await client.get("/api/admin/users")
                    assert client._csrf_token == "new-csrf"


class TestRetryOn403Csrf:
    @pytest.mark.asyncio
    async def test_403_csrf_triggers_token_refresh_and_retry(self) -> None:
        csrf_resp = _resp(403, body={"message": "CSRF token invalid"})
        ok_resp = _resp(200, body={"ok": True})

        with _patch_login("old-csrf"):
            with _patch_fetch_csrf("fresh-csrf"):
                async with PathToHiredClient(_CREDS) as client:
                    client._http.request = AsyncMock(side_effect=[csrf_resp, ok_resp])
                    resp = await client.post("/api/admin/users/1/approve")

        assert resp.status_code == 200
        assert client._http.request.call_count == 2

    @pytest.mark.asyncio
    async def test_403_non_csrf_is_not_retried(self) -> None:
        forbidden_resp = _resp(403, body={"message": "insufficient permissions"})

        with _patch_login("csrf"):
            async with PathToHiredClient(_CREDS) as client:
                client._http.request = AsyncMock(return_value=forbidden_resp)
                resp = await client.get("/api/admin/users")

        assert resp.status_code == 403
        assert client._http.request.call_count == 1
