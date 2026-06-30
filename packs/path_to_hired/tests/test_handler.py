"""Tests for handler.py — action dispatch, approval gating, and error surfaces.

Covers:
- Happy path per action category (read, single-target write, gated).
- Approval positive path: gated verb with --approved executes.
- Approval negative path: gated verb without --approved returns approval_required.
- 401-then-refresh flow (via client integration).
- 403-then-csrf-refresh flow.
- 429 surface (no retry storm).
- Missing/unreadable secret file surfaced as clean error.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import handler
import pytest
from handler import ALL_VERBS, APPROVAL_GATED, dispatch
from helpers.secrets import CredentialError, Credentials

pytestmark = pytest.mark.unit

_CREDS = Credentials(base_url="https://api.pathtohired.com", username="u", password="p")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with defaults matching the parser."""
    defaults = {
        "approved": False,
        "user_id": None,
        "post_id": None,
        "email": None,
        "username": None,
        "role": None,
        "title": None,
        "content": None,
        "excerpt": None,
        "tags": None,
        "reason": None,
        "name": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _resp(status: int, body=None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body or {})
    r.text = text
    return r


def _patch_creds():
    return patch("handler.load_credentials", return_value=_CREDS)


def _patch_client_method(method: str, response: MagicMock):
    """Patch a method on PathToHiredClient instances."""
    return patch.object(
        handler.PathToHiredClient,
        method,
        new=AsyncMock(return_value=response),
    )


class _FakeClient:
    """Minimal async context manager stub for the client."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def _get_resp(self, key: str) -> MagicMock:
        return self._responses.get(key, _resp(200, {}))

    async def get(self, path, **kw):
        return self._get_resp("get")

    async def post(self, path, **kw):
        return self._get_resp("post")

    async def put(self, path, **kw):
        return self._get_resp("put")

    async def delete(self, path, **kw):
        return self._get_resp("delete")


def _patch_client(responses: dict):
    return patch("handler.PathToHiredClient", return_value=_FakeClient(responses))


# ---------------------------------------------------------------------------
# Pack shape
# ---------------------------------------------------------------------------


class TestPackShape:
    def test_approval_gated_set_matches_pack_yaml(self) -> None:
        from pathlib import Path

        from router.packs.loader import load_pack

        pack_dir = Path(__file__).resolve().parents[1]
        pack = load_pack(pack_dir)
        assert pack.name == "path_to_hired"
        assert pack.needs == []
        assert set(pack.approve) == APPROVAL_GATED

    def test_all_verbs_covered(self) -> None:
        assert len(ALL_VERBS) == 19

    def test_prompt_documents_gated_verbs(self) -> None:
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "prompt.md").read_text()
        for verb in APPROVAL_GATED:
            assert verb in text, f"prompt.md missing gated verb: {verb}"
        assert "draft-approval" in text
        assert "cancelled" in text


# ---------------------------------------------------------------------------
# Approval gating
# ---------------------------------------------------------------------------


class TestApprovalGating:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", sorted(APPROVAL_GATED))
    async def test_gated_verb_without_approved_returns_approval_required(self, verb: str) -> None:
        args = _args(verb=verb, user_id="1", post_id="1", email="a@b.com", role="member")
        result = await dispatch(args)
        assert result["status"] == "approval_required"
        assert result["pack"] == "path_to_hired"
        assert result["action_verb"] == verb
        assert "draft_id" in result
        assert "payload" in result

    @pytest.mark.asyncio
    async def test_publish_blog_post_with_approved_executes(self) -> None:
        args = _args(verb="publish_blog_post", post_id="7", approved=True)
        ok = _resp(200, {"id": 7, "published": True})
        with _patch_creds():
            with _patch_client({"post": ok}):
                result = await dispatch(args)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_delete_user_with_approved_executes(self) -> None:
        args = _args(verb="delete_user", user_id="42", approved=True)
        ok = _resp(200, {})
        with _patch_creds():
            with _patch_client({"delete": ok}):
                result = await dispatch(args)
        assert result["status"] == "ok"
        assert result["data"]["deleted"] is True

    @pytest.mark.asyncio
    async def test_gated_payload_contains_relevant_args(self) -> None:
        args = _args(verb="update_user_role", user_id="5", role="admin")
        result = await dispatch(args)
        assert result["payload"]["user_id"] == "5"
        assert result["payload"]["role"] == "admin"


# ---------------------------------------------------------------------------
# Read-only actions
# ---------------------------------------------------------------------------


class TestReadActions:
    @pytest.mark.asyncio
    async def test_list_users(self) -> None:
        users_data = {"users": [{"id": 1, "email": "a@b.com"}]}
        with _patch_creds():
            with _patch_client({"get": _resp(200, users_data)}):
                result = await dispatch(_args(verb="list_users"))
        assert result["status"] == "ok"
        assert result["data"] == users_data

    @pytest.mark.asyncio
    async def test_list_pending_users(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(200, {"users": []})}):
                result = await dispatch(_args(verb="list_pending_users"))
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_user_by_id(self) -> None:
        user_list = [{"id": 42, "email": "user@example.com"}, {"id": 99, "email": "other@example.com"}]
        with _patch_creds():
            with _patch_client({"get": _resp(200, user_list)}):
                result = await dispatch(_args(verb="get_user", user_id="42"))
        assert result["status"] == "ok"
        assert result["data"]["id"] == 42

    @pytest.mark.asyncio
    async def test_get_user_by_email(self) -> None:
        user_list = [{"id": 10, "email": "alice@example.com"}]
        with _patch_creds():
            with _patch_client({"get": _resp(200, user_list)}):
                result = await dispatch(_args(verb="get_user", email="ALICE@EXAMPLE.COM"))
        assert result["status"] == "ok"
        assert result["data"]["id"] == 10

    @pytest.mark.asyncio
    async def test_get_user_by_email_with_null_email_users_before_target(self) -> None:
        user_list = [{"id": 1, "email": None}, {"id": 2, "email": "target@example.com"}]
        with _patch_creds():
            with _patch_client({"get": _resp(200, user_list)}):
                result = await dispatch(_args(verb="get_user", email="target@example.com"))
        assert result["status"] == "ok"
        assert result["data"]["id"] == 2

    @pytest.mark.asyncio
    async def test_get_user_not_found(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(200, [])}):
                result = await dispatch(_args(verb="get_user", user_id="999"))
        assert result["status"] == "error"
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_get_user_requires_id_or_email(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(200, [])}):
                result = await dispatch(_args(verb="get_user"))
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_list_blog_posts(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(200, {"posts": []})}):
                result = await dispatch(_args(verb="list_blog_posts"))
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_blog_post(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(200, {"id": 7, "title": "Hello"})}):
                result = await dispatch(_args(verb="get_blog_post", post_id="7"))
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_preview_blog_post(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(200, {"html": "<p>hello</p>"})}):
                result = await dispatch(_args(verb="preview_blog_post", post_id="7"))
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Single-target write actions (ungated)
# ---------------------------------------------------------------------------


class TestUngatedWriteActions:
    @pytest.mark.asyncio
    async def test_approve_user(self) -> None:
        with _patch_creds():
            with _patch_client({"post": _resp(200, {"status": "approved"})}):
                result = await dispatch(_args(verb="approve_user", user_id="5"))
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unlock_user(self) -> None:
        with _patch_creds():
            with _patch_client({"post": _resp(200, {})}):
                result = await dispatch(_args(verb="unlock_user", user_id="5"))
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_create_blog_post_draft_forces_published_false(self) -> None:
        created = {"id": 1, "title": "New Post", "published": False}
        captured_body = {}

        class _CapturingClient(_FakeClient):
            async def post(self, path, json=None, **kw):
                captured_body.update(json or {})
                return _resp(200, created)

        with _patch_creds():
            with patch("handler.PathToHiredClient", return_value=_CapturingClient({})):
                result = await dispatch(_args(verb="create_blog_post_draft", title="New Post"))

        assert result["status"] == "ok"
        assert captured_body.get("published") is False

    @pytest.mark.asyncio
    async def test_create_blog_post_draft_requires_title(self) -> None:
        with _patch_creds():
            with _patch_client({"post": _resp(200, {})}):
                result = await dispatch(_args(verb="create_blog_post_draft"))
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_update_blog_post_draft_happy_path(self) -> None:
        fetch_resp = _resp(200, {"id": 3, "published": False})
        update_resp = _resp(200, {"id": 3, "title": "Updated"})

        class _TwoCallClient(_FakeClient):
            def __init__(self):
                self._get_called = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, path, **kw):
                return fetch_resp

            async def put(self, path, **kw):
                return update_resp

        with _patch_creds():
            with patch("handler.PathToHiredClient", return_value=_TwoCallClient()):
                result = await dispatch(_args(verb="update_blog_post_draft", post_id="3", title="Updated"))
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_blog_post_draft_refuses_if_published(self) -> None:
        fetch_resp = _resp(200, {"id": 3, "published": True})

        class _PublishedClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, path, **kw):
                return fetch_resp

        with _patch_creds():
            with patch("handler.PathToHiredClient", return_value=_PublishedClient()):
                result = await dispatch(_args(verb="update_blog_post_draft", post_id="3", title="X"))
        assert result["status"] == "error"
        assert "published" in result["message"].lower()


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


class TestErrorSurfaces:
    @pytest.mark.asyncio
    async def test_missing_credentials_returns_clean_error(self) -> None:
        with patch("handler.load_credentials", side_effect=CredentialError("credentials file not found")):
            result = await dispatch(_args(verb="list_users", approved=True))
        assert result["status"] == "error"
        assert "credentials" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_429_from_login_surfaced_cleanly(self) -> None:
        from helpers.auth import RateLimitError

        with _patch_creds():
            with patch("handler.PathToHiredClient") as MockClient:
                instance = MagicMock()
                instance.__aenter__ = AsyncMock(side_effect=RateLimitError("Login rate-limited (429)"))
                instance.__aexit__ = AsyncMock(return_value=False)
                MockClient.return_value = instance
                result = await dispatch(_args(verb="list_users"))
        assert result["status"] == "error"
        assert "rate" in result["message"].lower() or "429" in result["message"]

    @pytest.mark.asyncio
    async def test_http_error_response_returned_structured(self) -> None:
        with _patch_creds():
            with _patch_client({"get": _resp(500, text="internal error")}):
                result = await dispatch(_args(verb="list_users"))
        assert result["status"] == "error"
        assert result.get("http_status") == 500


# ---------------------------------------------------------------------------
# 401 and 403 retry integration (via client internals patched at auth layer)
# ---------------------------------------------------------------------------


class TestAuthRetryIntegration:
    @pytest.mark.asyncio
    async def test_401_then_refresh_succeeds(self) -> None:
        """Verify PathToHiredClient transparently retries after 401."""

        first = _resp(401)
        ok = _resp(200, {"users": []})

        with _patch_creds():
            with patch("helpers.client.refresh_session", new=AsyncMock(return_value="new-tok")):
                with patch("helpers.client.login", new=AsyncMock(return_value="initial-tok")):
                    async with handler.PathToHiredClient(_CREDS) as client:
                        client._http.request = AsyncMock(side_effect=[first, ok])
                        resp = await client.get("/api/admin/users")
        assert resp.status_code == 200
        assert client._http.request.call_count == 2

    @pytest.mark.asyncio
    async def test_403_csrf_then_refresh_succeeds(self) -> None:
        csrf_err = _resp(403, body={"message": "CSRF token invalid"})
        ok = _resp(200, {"posts": []})

        with patch("helpers.client.fetch_csrf", new=AsyncMock(return_value="fresh-tok")):
            with patch("helpers.client.login", new=AsyncMock(return_value="initial-tok")):
                async with handler.PathToHiredClient(_CREDS) as client:
                    client._http.request = AsyncMock(side_effect=[csrf_err, ok])
                    resp = await client.get("/api/admin/blog/posts")
        assert resp.status_code == 200
        assert client._http.request.call_count == 2
