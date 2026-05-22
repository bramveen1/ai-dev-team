"""Unit tests for router.internal_api — the POST /internal/drafts endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from router.approvals.store import DraftStore
from router.internal_api import build_app, check_token_configured

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PAYLOAD: dict = {
    "agent": "sam",
    "repo": "bramveen1/ai-dev-team",
    "issue": 247,
    "title": "Implement dispatch endpoint",
    "model": "sonnet",
    "persona": "dev",
    "supervision_mode": "poll",
    "budget_seconds": 1800,
    "gate_reason": "high-risk change",
    "thread_ts": "1705700000.000100",
    "channel": "C12345",
}


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_internal_api.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture
def slack_client():
    """Mock Slack WebClient that returns a successful chat_postMessage result."""
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1705700001.000200"})
    return client


@pytest.fixture
def client_resolver(slack_client):
    """Returns a resolver that always yields the mock Slack client."""
    return lambda agent_name: slack_client


@pytest.fixture
def null_client_resolver():
    """Returns a resolver that always yields None (no Slack client)."""
    return lambda agent_name: None


async def _make_test_client(store, client_resolver, token: str | None = "secret-token"):
    """Build an aiohttp TestClient for the internal API app."""
    app = build_app(store, client_resolver)
    return TestClient(TestServer(app))


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_no_auth_header(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post("/internal/drafts", json=_VALID_PAYLOAD)
        assert resp.status == 401
        body = await resp.json()
        assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_401_wrong_token(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=_VALID_PAYLOAD,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401
        body = await resp.json()
        assert body["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_401_malformed_auth_scheme(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=_VALID_PAYLOAD,
            headers={"Authorization": "Token secret-token"},
        )
        assert resp.status == 401


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_422_bad_model(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    bad = {**_VALID_PAYLOAD, "model": "gpt-4"}
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=bad,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 422
        body = await resp.json()
        assert "model" in body["error"]


@pytest.mark.asyncio
async def test_422_extra_field(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    bad = {**_VALID_PAYLOAD, "unexpected_key": "value"}
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=bad,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 422
        body = await resp.json()
        assert "unknown field" in body["error"]


@pytest.mark.asyncio
async def test_422_bad_persona(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    bad = {**_VALID_PAYLOAD, "persona": "admin"}
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=bad,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 422
        body = await resp.json()
        assert "persona" in body["error"]


@pytest.mark.asyncio
async def test_422_bad_supervision_mode(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    bad = {**_VALID_PAYLOAD, "supervision_mode": "auto"}
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=bad,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 422
        body = await resp.json()
        assert "supervision_mode" in body["error"]


@pytest.mark.asyncio
async def test_422_missing_field(store, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    bad = {k: v for k, v in _VALID_PAYLOAD.items() if k != "channel"}
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=bad,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 422
        body = await resp.json()
        assert "missing" in body["error"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_200_happy_path_writes_draft_and_posts_card(store, slack_client, client_resolver, monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=_VALID_PAYLOAD,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert "draft_id" in body
        assert body["card_ts"] == "1705700001.000200"

    # Draft must be persisted in the store
    drafts = store.list_by_status("pending")
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.draft_id == body["draft_id"]
    assert draft.agent_name == "sam"
    assert draft.slack_channel == "C12345"
    assert draft.slack_message_ts == "1705700001.000200"
    assert draft.capability_type == "dispatch"

    # Slack card must have been posted
    slack_client.chat_postMessage.assert_awaited_once()
    call_kwargs = slack_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C12345"
    assert call_kwargs["thread_ts"] == _VALID_PAYLOAD["thread_ts"]
    assert "blocks" in call_kwargs


# ---------------------------------------------------------------------------
# Slack failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_502_slack_failure_does_not_persist_draft(store, slack_client, client_resolver, monkeypatch):
    """A draft without an approval card is unreachable; persisting it would leak
    orphans the operator has no recovery path for. The caller re-POSTs and gets
    a fresh draft_id instead."""
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "secret-token")
    slack_client.chat_postMessage = AsyncMock(side_effect=RuntimeError("Slack is down"))

    async with await _make_test_client(store, client_resolver) as client:
        resp = await client.post(
            "/internal/drafts",
            json=_VALID_PAYLOAD,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 502
        body = await resp.json()
        assert body["error"] == "slack_post_failed"
        # No draft_id leak — caller has nothing to retry against, must POST anew.
        assert "draft_id" not in body

    # Store must be empty: nothing to clean up if Slack post failed.
    drafts = store.list_by_status("pending")
    assert drafts == []


# ---------------------------------------------------------------------------
# check_token_configured
# ---------------------------------------------------------------------------


def test_check_token_configured_raises_when_missing(monkeypatch):
    monkeypatch.delenv("ROUTER_INTERNAL_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ROUTER_INTERNAL_TOKEN"):
        check_token_configured()


def test_check_token_configured_passes_when_set(monkeypatch):
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "any-value")
    check_token_configured()  # must not raise
