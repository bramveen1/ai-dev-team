"""Unit tests for router/internal_api.py.

Covers:
- 401 on missing / wrong Bearer token
- 422 on bad model, extra field, bad persona/supervision_mode, missing field
- 200 happy path (draft persisted + Slack mocked)
- 502 when Slack fails but draft was already persisted
- GET /internal/drafts?agent=<name> happy path
- GET /internal/drafts missing ?agent param → 400
- check_token_configured() fail-fast
- POST /internal/status: auth, missing fields, happy path via agent adapter,
  precheck-fail / no-adapter / send-failure degrade to {"posted": false} (#707)
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

import router.internal_api as _api_mod
from router.approvals.adapters.slack import SlackApprovalAdapter
from router.approvals.store import Draft, DraftStore
from router.internal_api import (
    build_internal_app,
    check_token_configured,
    configure,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "test-secret-token"

_VALID_BODY: dict = {
    "agent": "sam",
    "repo": "bramveen1/ai-dev-team",
    "issue": 42,
    "title": "Fix regression",
    "model": "sonnet",
    "persona": "dev",
    "supervision_mode": "poll",
    "budget_seconds": 1800,
    "thread_ts": "1705700000.000100",
    "channel": "C12345",
}


@pytest.fixture()
def store(tmp_path):
    db_path = str(tmp_path / "drafts.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture()
def slack_client():
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "1705700001.000200"})
    return client


@pytest.fixture()
def _reset_module():
    """Ensure module-level state is reset after each test."""
    yield
    importlib.reload(_api_mod)


@pytest_asyncio.fixture()
async def test_client(store, slack_client, _reset_module):
    """Build a live aiohttp TestClient with the internal app wired up."""
    configure(
        store=store,
        client_resolver=lambda agent: slack_client if agent == "sam" else None,
    )
    with patch("router.internal_api._get_expected_token", return_value=_TOKEN):
        app = build_internal_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        yield client
        await client.close()


@pytest_asyncio.fixture()
async def test_client_discord(store, _reset_module):
    """Build a live aiohttp TestClient with Discord token resolver wired up."""
    configure(
        store=store,
        client_resolver=lambda agent: None,
        discord_token_resolver=lambda agent: "discord-bot-token-sam" if agent == "sam" else None,
    )
    with patch("router.internal_api._get_expected_token", return_value=_TOKEN):
        app = build_internal_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        yield client
        await client.close()


@pytest.fixture()
def discord_adapter():
    """Fake DiscordAdapter for the /internal/status tests (#707)."""
    adapter = MagicMock()
    adapter.can_post = MagicMock(return_value=True)
    adapter.send_message = AsyncMock()
    return adapter


@pytest_asyncio.fixture()
async def test_client_status(store, discord_adapter, _reset_module):
    """Build a live aiohttp TestClient with a Discord adapter resolver wired up."""
    configure(
        store=store,
        client_resolver=lambda agent: None,
        discord_adapter_resolver=lambda agent: discord_adapter if agent == "sam" else None,
    )
    with patch("router.internal_api._get_expected_token", return_value=_TOKEN):
        app = build_internal_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        yield client
        await client.close()


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_no_token_returns_401(test_client):
    resp = await test_client.post("/internal/drafts", json=_VALID_BODY)
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "unauthorized"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_wrong_token_returns_401(test_client):
    resp = await test_client.post(
        "/internal/drafts",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_drafts_no_token_returns_401(test_client):
    resp = await test_client.get("/internal/drafts?agent=sam")
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Validation (422) tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_bad_model_returns_422(test_client):
    body = {**_VALID_BODY, "model": "gpt-4"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "invalid_model"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_bad_persona_returns_422(test_client):
    body = {**_VALID_BODY, "persona": "evil"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "invalid_persona"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_bad_supervision_mode_returns_422(test_client):
    body = {**_VALID_BODY, "supervision_mode": "realtime"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "invalid_supervision_mode"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_stream_supervision_mode_returns_422(test_client):
    """'stream' has no producer in the handler and must be rejected."""
    body = {**_VALID_BODY, "supervision_mode": "stream"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "invalid_supervision_mode"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_inline_supervision_mode_accepted(test_client, store, slack_client):
    """'inline' is a first-class supervision mode produced by the dispatch handler and must be accepted."""
    body = {**_VALID_BODY, "supervision_mode": "inline"}
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch.object(
            SlackApprovalAdapter,
            "render_approval_card",
            return_value={"blocks": []},
        ),
    ):
        resp = await test_client.post(
            "/internal/drafts",
            json=body,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert "draft_id" in data
    draft = store.get(data["draft_id"])
    assert draft is not None
    assert draft.payload["supervision_mode"] == "inline"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_transport_fields_accepted(test_client, store, slack_client):
    """The dispatch pack sends ``transport``/``conversation_id`` unconditionally
    since #664 (TransportRef). The router must accept them, not reject with
    422 unknown_fields — otherwise every dispatch breaks (regression: #561)."""
    body = {**_VALID_BODY, "transport": "slack", "conversation_id": ""}
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch.object(
            SlackApprovalAdapter,
            "render_approval_card",
            return_value={"blocks": []},
        ),
    ):
        resp = await test_client.post(
            "/internal/drafts",
            json=body,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert "draft_id" in data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_discord_transport_propagated_to_payload(test_client_discord, store):
    """#665/#680: transport/conversation_id in the request body must be persisted in
    draft_payload so the approve→execute path can reconstruct the originating
    transport and post status back to Discord rather than Slack."""
    body = {
        **_VALID_BODY,
        "transport": "discord",
        "conversation_id": "discord:123:456:789",
    }
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch("router.internal_api._build_and_post_discord_card", return_value="discord_msg_id_abc"),
    ):
        resp = await test_client_discord.post(
            "/internal/drafts",
            json=body,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 200
    data = await resp.json()
    draft = store.get(data["draft_id"])
    assert draft is not None
    assert draft.payload.get("transport") == "discord"
    assert draft.payload.get("conversation_id") == "discord:123:456:789"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_slack_transport_not_stored_when_empty(test_client, store, slack_client):
    """Empty conversation_id should not be stored in payload (falsy → skip)."""
    body = {**_VALID_BODY, "transport": "slack", "conversation_id": ""}
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch.object(
            SlackApprovalAdapter,
            "render_approval_card",
            return_value={"blocks": []},
        ),
    ):
        resp = await test_client.post(
            "/internal/drafts",
            json=body,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 200
    data = await resp.json()
    draft = store.get(data["draft_id"])
    assert draft is not None
    # transport="slack" is non-empty so it's stored; conversation_id="" is skipped
    assert draft.payload.get("transport") == "slack"
    assert "conversation_id" not in draft.payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_extra_field_returns_422(test_client):
    body = {**_VALID_BODY, "unexpected_extra": "value"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "unknown_fields"
    assert "unexpected_extra" in data["fields"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_missing_required_field_returns_422(test_client):
    body = {k: v for k, v in _VALID_BODY.items() if k != "channel"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "missing_fields"
    assert "channel" in data["fields"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_unknown_agent_returns_422(test_client):
    body = {**_VALID_BODY, "agent": "unknown-agent"}
    resp = await test_client.post("/internal/drafts", json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "unknown_agent"


# ---------------------------------------------------------------------------
# Happy path (200)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_happy_path(test_client, store, slack_client):
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch.object(
            SlackApprovalAdapter,
            "render_approval_card",
            return_value={"blocks": []},
        ),
    ):
        resp = await test_client.post(
            "/internal/drafts",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert "draft_id" in data
    assert "card_ts" in data
    assert data["card_ts"] == "1705700001.000200"

    # Draft was persisted in the store.
    draft = store.get(data["draft_id"])
    assert draft is not None
    assert draft.agent_name == "sam"
    assert draft.slack_channel == "C12345"
    assert draft.slack_message_ts == "1705700001.000200"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_persist_before_post(test_client, store, slack_client):
    """Draft row must exist in the DB even when Slack post fails (persist-before-post)."""
    slack_client.chat_postMessage.side_effect = Exception("Slack is down")

    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch.object(
            SlackApprovalAdapter,
            "render_approval_card",
            return_value={"blocks": []},
        ),
    ):
        resp = await test_client.post(
            "/internal/drafts",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 502
    data = await resp.json()
    assert data["error"] == "slack_post_failed"
    assert "draft_id" in data

    # Draft MUST still be in the store despite Slack failure.
    draft = store.get(data["draft_id"])
    assert draft is not None
    assert draft.slack_message_ts == ""  # Empty because Slack never succeeded.


# ---------------------------------------------------------------------------
# GET /internal/drafts
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_drafts_happy_path(test_client, store):
    # Pre-populate a pending draft directly in the store.
    from router.approvals.store import Draft

    now = datetime.now(timezone.utc)
    draft = Draft(
        draft_id="abc12345",
        agent_name="sam",
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload={"issue_url": "https://github.com/owner/repo/issues/1"},
        slack_channel="C12345",
        slack_message_ts="",
        created_at=now,
        expires_at=None,
    )
    store.create(draft)

    resp = await test_client.get(
        "/internal/drafts?agent=sam",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["agent"] == "sam"
    assert len(data["drafts"]) == 1
    assert data["drafts"][0]["draft_id"] == "abc12345"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_drafts_missing_agent_param_returns_400(test_client):
    resp = await test_client.get(
        "/internal/drafts",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "missing_query_param"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_drafts_empty_for_unknown_agent(test_client):
    resp = await test_client.get(
        "/internal/drafts?agent=nobody",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["drafts"] == []


# ---------------------------------------------------------------------------
# check_token_configured
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_token_configured_raises_when_missing(monkeypatch):
    monkeypatch.delenv("ROUTER_INTERNAL_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        check_token_configured()


@pytest.mark.unit
def test_check_token_configured_passes_when_set(monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "some-token")
    check_token_configured()  # Should not raise.


# ---------------------------------------------------------------------------
# Discord card posting (#680)
# ---------------------------------------------------------------------------


def _make_aiohttp_post_mock(*, status: int = 200, json_body: dict | None = None):
    """Build a nested aiohttp mock: ClientSession → post → response, and
    return (patch_target, session_mock) so the caller can inspect the call
    args made to ``session.post`` after the code under test runs."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body if json_body is not None else {"id": "discord_posted"})
    resp.text = AsyncMock(return_value="")

    resp_ctx = MagicMock()
    resp_ctx.__aenter__ = AsyncMock(return_value=resp)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=resp_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    return session_ctx, session


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_and_post_discord_card_sends_embed_and_components():
    """#682: the Discord card must post an embed + interactive button row —
    a text-only ``content`` post has no Approve control to click."""
    from router.approvals.button_resolver import ButtonSpec
    from router.internal_api import _build_and_post_discord_card

    draft = Draft(
        draft_id="d1",
        agent_name="sam",
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload={"issue_url": "https://github.com/o/r/issues/1"},
        slack_channel="discord:1:2:3",
        slack_message_ts="",
        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )

    session_ctx, session = _make_aiohttp_post_mock()
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch(
            "router.internal_api.resolve_buttons",
            return_value=[ButtonSpec(action_id="approve_send", text="Dispatch_issue", style="primary")],
        ),
        patch("aiohttp.ClientSession", return_value=session_ctx),
    ):
        result = await _build_and_post_discord_card(
            draft=draft,
            conversation_id="discord:1:2:3",
            token="fake-token",
        )

    assert result == "discord_posted"
    session.post.assert_called_once()
    call_kwargs = session.post.call_args.kwargs
    posted = json.loads(call_kwargs["data"])
    assert "content" in posted
    assert "embeds" in posted and len(posted["embeds"]) == 1
    assert "components" in posted
    [row] = posted["components"]
    [button] = row["components"]
    assert button["custom_id"] == "approval:approve_send:d1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_discord_card_posted_returns_card_ref(test_client_discord, store):
    """#680: Discord-origin draft posts card via Discord adapter, returns card_ref."""
    body = {
        **_VALID_BODY,
        "transport": "discord",
        "conversation_id": "discord:111:222:333",
    }
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch("router.internal_api._build_and_post_discord_card", return_value="dc_msg_99"),
    ):
        resp = await test_client_discord.post(
            "/internal/drafts",
            json=body,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert data.get("card_ref") == "dc_msg_99"
    assert "draft_id" in data
    assert "card_ts" not in data
    draft = store.get(data["draft_id"])
    assert draft is not None
    assert draft.slack_message_ts == "dc_msg_99"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_discord_no_token_returns_422(test_client_discord):
    """#680: Discord draft for an unconfigured agent returns 422 discord_agent_not_configured."""
    body = {
        **_VALID_BODY,
        "agent": "lisa",
        "transport": "discord",
        "conversation_id": "discord:1:2:3",
    }
    resp = await test_client_discord.post(
        "/internal/drafts",
        json=body,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "discord_agent_not_configured"
    assert data.get("agent") == "lisa"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_draft_discord_post_fails_returns_502(test_client_discord, store):
    """#680: When Discord card post fails, 502 is returned but draft is persisted."""
    body = {
        **_VALID_BODY,
        "transport": "discord",
        "conversation_id": "discord:1:2:3",
    }
    with (
        patch("router.internal_api.discover_packs", return_value={}),
        patch("router.internal_api.resolve_buttons", return_value=[]),
        patch("router.internal_api._build_and_post_discord_card", return_value=None),
    ):
        resp = await test_client_discord.post(
            "/internal/drafts",
            json=body,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status == 502
    data = await resp.json()
    assert data["error"] == "discord_post_failed"
    assert "draft_id" in data
    draft = store.get(data["draft_id"])
    assert draft is not None
    assert draft.slack_message_ts == ""


# ---------------------------------------------------------------------------
# POST /internal/status — Discord worker status via agent identity (#707)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_no_token_returns_401(test_client_status):
    resp = await test_client_status.post(
        "/internal/status",
        json={"agent": "sam", "conversation_id": "discord:1:2:3", "text": "started"},
    )
    assert resp.status == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_wrong_token_returns_401(test_client_status):
    resp = await test_client_status.post(
        "/internal/status",
        json={"agent": "sam", "conversation_id": "discord:1:2:3", "text": "started"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_missing_fields_returns_422(test_client_status):
    resp = await test_client_status.post(
        "/internal/status",
        json={"agent": "sam"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 422
    data = await resp.json()
    assert data["error"] == "missing_fields"
    assert "conversation_id" in data["fields"]
    assert "text" in data["fields"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_happy_path_posts_via_agent_adapter(test_client_status, discord_adapter):
    resp = await test_client_status.post(
        "/internal/status",
        json={"agent": "sam", "conversation_id": "discord:1:2:3", "text": "dispatch started"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"posted": True}

    discord_adapter.can_post.assert_called_once_with("discord:1:2:3")
    discord_adapter.send_message.assert_awaited_once()
    outbound = discord_adapter.send_message.await_args.args[0]
    assert outbound.text == "dispatch started"
    assert outbound.conversation_ref == "discord:1:2:3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_no_adapter_degrades_to_posted_false(test_client_status, caplog):
    """Unknown agent → no adapter resolved → structured warning, not a 5xx."""
    import logging

    with caplog.at_level(logging.WARNING, logger="router.internal_api"):
        resp = await test_client_status.post(
            "/internal/status",
            json={"agent": "lisa", "conversation_id": "discord:1:2:3", "text": "started"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"posted": False}
    assert any("worker_status_unpostable" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_precheck_fail_degrades_to_posted_false(test_client_status, discord_adapter, caplog):
    """Adapter present but not a member of the target channel/thread → degrade, never a raw 403."""
    import logging

    discord_adapter.can_post = MagicMock(return_value=False)

    with caplog.at_level(logging.WARNING, logger="router.internal_api"):
        resp = await test_client_status.post(
            "/internal/status",
            json={"agent": "sam", "conversation_id": "discord:1:2:3", "text": "started"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"posted": False}
    discord_adapter.send_message.assert_not_awaited()
    assert any("worker_status_unpostable" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_status_send_failure_degrades_to_posted_false(test_client_status, discord_adapter):
    """A send_message exception degrades to {"posted": false} instead of a 5xx."""
    discord_adapter.send_message = AsyncMock(side_effect=RuntimeError("boom"))

    resp = await test_client_status.post(
        "/internal/status",
        json={"agent": "sam", "conversation_id": "discord:1:2:3", "text": "started"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"posted": False}
