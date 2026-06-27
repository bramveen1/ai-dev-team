"""Unit tests for router/internal_api.py.

Covers:
- 401 on missing / wrong Bearer token
- 422 on bad model, extra field, bad persona/supervision_mode, missing field
- 200 happy path (draft persisted + Slack mocked)
- 502 when Slack fails but draft was already persisted
- GET /internal/drafts?agent=<name> happy path
- GET /internal/drafts missing ?agent param → 400
- check_token_configured() fail-fast
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

import router.internal_api as _api_mod
from router.approvals.adapters.slack import SlackApprovalAdapter
from router.approvals.store import DraftStore
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
