"""Unit tests for router.slack_users — workspace user directory + mention IDs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import slack_users

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache():
    slack_users._cache.clear()
    slack_users._cache_at = 0.0
    slack_users._warned_unavailable = False
    yield
    slack_users._cache.clear()
    slack_users._cache_at = 0.0
    slack_users._warned_unavailable = False


def _member(uid: str, display: str = "", real: str = "", handle: str = "", deleted: bool = False) -> dict:
    return {
        "id": uid,
        "name": handle,
        "deleted": deleted,
        "profile": {"display_name": display, "real_name": real},
    }


def _client_with_pages(*pages: dict) -> MagicMock:
    client = MagicMock()
    client.users_list = AsyncMock(side_effect=list(pages))
    return client


class TestWorkspaceUserIds:
    @pytest.mark.asyncio
    async def test_maps_display_real_and_handle_names(self):
        client = _client_with_pages(
            {"members": [_member("U1", display="Dev Lisa", real="Lisa Example", handle="lisa")]}
        )
        result = await slack_users.workspace_user_ids(client)
        assert result == {"dev lisa": "U1", "lisa example": "U1", "lisa": "U1"}

    @pytest.mark.asyncio
    async def test_follows_pagination_cursor(self):
        client = _client_with_pages(
            {"members": [_member("U1", display="Alpha")], "response_metadata": {"next_cursor": "c2"}},
            {"members": [_member("U2", display="Beta")]},
        )
        result = await slack_users.workspace_user_ids(client)
        assert result == {"alpha": "U1", "beta": "U2"}
        assert client.users_list.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_deleted_and_short_names(self):
        client = _client_with_pages(
            {"members": [_member("U1", display="Gone", deleted=True), _member("U2", display="x", handle="ok")]}
        )
        result = await slack_users.workspace_user_ids(client)
        assert result == {"ok": "U2"}

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_uses_cache(self):
        client = _client_with_pages({"members": [_member("U1", display="Alpha")]})
        await slack_users.workspace_user_ids(client)
        await slack_users.workspace_user_ids(client)
        assert client.users_list.await_count == 1

    @pytest.mark.asyncio
    async def test_api_failure_is_soft_and_returns_last_good(self):
        client = _client_with_pages({"members": [_member("U1", display="Alpha")]})
        first = await slack_users.workspace_user_ids(client)
        assert first == {"alpha": "U1"}

        slack_users._cache_at = -10_000.0  # force staleness past the TTL
        failing = MagicMock()
        failing.users_list = AsyncMock(side_effect=RuntimeError("missing_scope"))
        assert await slack_users.workspace_user_ids(failing) == {"alpha": "U1"}

    @pytest.mark.asyncio
    async def test_non_async_client_yields_empty(self):
        # A bare MagicMock (legacy tests, misconfigured client) must not raise.
        assert await slack_users.workspace_user_ids(MagicMock()) == {}

    @pytest.mark.asyncio
    async def test_async_mock_client_yields_empty_without_stray_coroutines(self):
        # An AsyncMock client returns mock payloads, not dicts — the directory
        # must come back empty without touching mock attributes (which would
        # fabricate never-awaited coroutines and trip pytest's warning check).
        client = AsyncMock()
        assert await slack_users.workspace_user_ids(client) == {}
        client.users_list.assert_awaited_once()


class TestOutboundMentionIds:
    @pytest.mark.asyncio
    async def test_personas_overlay_workspace_names(self):
        client = _client_with_pages({"members": [_member("U_WS", display="Lisa")]})
        with (
            patch("router.slack_users.get_agent_map", return_value={"lisa": {"name": "Lisa"}}),
            patch.dict("router.runtime.bot_user_id_by_agent", {"lisa": "U_BOT"}, clear=True),
        ):
            result = await slack_users.outbound_mention_ids(client)
        # Persona resolution wins over the workspace entry for the same name.
        assert result["lisa"] == "U_BOT"

    @pytest.mark.asyncio
    async def test_personas_resolve_without_users_list(self):
        with (
            patch("router.slack_users.get_agent_map", return_value={"sam": {"name": "Sam"}}),
            patch.dict("router.runtime.bot_user_id_by_agent", {"sam": "U_SAM"}, clear=True),
        ):
            result = await slack_users.outbound_mention_ids(MagicMock())
        assert result == {"sam": "U_SAM"}
