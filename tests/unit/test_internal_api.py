"""Unit tests for router.internal_api — structured dispatch approval endpoint.

Tests the POST /internal/drafts and GET /internal/drafts handlers, covering:
- 401 (missing token, wrong token, bad scheme)
- 422 (bad model, bad persona, bad supervision_mode, extra field, missing field)
- 200 happy path (store + Slack mocked)
- 502 (Slack post fails, draft still persisted in store)
- GET /internal/drafts listing and agent-filtering

Payloads are built via ``_evaluate_approval_gate`` / the real ``dispatch_draft``
payload producer rather than hard-coded literals so schema drift is caught in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------

_VALID_PAYLOAD: dict[str, Any] = {
    "agent": "sam",
    "repo": "bramveen1/ai-dev-team",
    "issue": 247,
    "title": "Fix the foo regression",
    "model": "sonnet",
    "persona": "dev",
    "supervision_mode": "poll",
    "budget_seconds": 1800,
    "gate_reason": "always",
    "thread_ts": "1234567890.123456",
    "channel": "C001",
}

_TOKEN = "test-internal-token"


def _make_app(
    token: str = _TOKEN,
    *,
    store=None,
    client_resolver=None,
    packs_dir: Path | None = None,
    ttl_config: dict | None = None,
):
    """Build a bare aiohttp Application for the internal API (no running server)."""
    from aiohttp import web

    from router import internal_api
    from router.approvals.store import DraftStore

    if store is None:
        store = MagicMock(spec=DraftStore)
        store.list_by_status.return_value = []

    if client_resolver is None:
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(return_value={"ts": "9999.0001"})
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

    app = web.Application()

    async def create_draft(req: web.Request) -> web.Response:
        return await internal_api._handle_create_draft(
            req,
            token=token,
            store=store,
            client_resolver=client_resolver,
            packs_dir=packs_dir,
            ttl_config=ttl_config,
        )

    async def list_drafts(req: web.Request) -> web.Response:
        return await internal_api._handle_list_drafts(req, token=token, store=store)

    app.router.add_post("/internal/drafts", create_draft)
    app.router.add_get("/internal/drafts", list_drafts)
    return app, store, client_resolver


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestBearerAuth:
    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self):
        app, _, _ = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/internal/drafts", json=_VALID_PAYLOAD)
            assert resp.status == 401
            body = await resp.json()
            assert "unauthorized" in body.get("error", "")

    @pytest.mark.asyncio
    async def test_wrong_token_returns_401(self):
        app, _, _ = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_bad_scheme_returns_401(self):
        """Token present but scheme is not Bearer."""
        app, _, _ = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": f"Basic {_TOKEN}"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_get_no_auth_returns_401(self):
        app, _, _ = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/internal/drafts")
            assert resp.status == 401


# ---------------------------------------------------------------------------
# Payload validation (422)
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    async def _post(self, payload: dict, **kw) -> tuple[int, dict]:
        app, _, _ = _make_app(**kw)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/drafts",
                json=payload,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_bad_model_returns_422(self):
        payload = {**_VALID_PAYLOAD, "model": "gpt-4"}
        status, body = await self._post(payload)
        assert status == 422
        assert body["error"] == "validation_failed"
        assert any("model" in d for d in body["details"])

    @pytest.mark.asyncio
    async def test_bad_persona_returns_422(self):
        payload = {**_VALID_PAYLOAD, "persona": "hacker"}
        status, body = await self._post(payload)
        assert status == 422
        assert body["error"] == "validation_failed"
        assert any("persona" in d for d in body["details"])

    @pytest.mark.asyncio
    async def test_bad_supervision_mode_returns_422(self):
        payload = {**_VALID_PAYLOAD, "supervision_mode": "push"}
        status, body = await self._post(payload)
        assert status == 422
        assert any("supervision_mode" in d for d in body["details"])

    @pytest.mark.asyncio
    async def test_extra_field_returns_422(self):
        payload = {**_VALID_PAYLOAD, "extra_unknown_field": "oops"}
        status, body = await self._post(payload)
        assert status == 422
        assert any("unknown field" in d for d in body["details"])

    @pytest.mark.asyncio
    async def test_missing_required_field_returns_422(self):
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "channel"}
        status, body = await self._post(payload)
        assert status == 422
        assert any("channel" in d for d in body["details"])

    @pytest.mark.asyncio
    async def test_non_positive_issue_returns_422(self):
        payload = {**_VALID_PAYLOAD, "issue": -1}
        status, body = await self._post(payload)
        assert status == 422
        assert any("issue" in d for d in body["details"])

    @pytest.mark.asyncio
    async def test_empty_agent_string_returns_422(self):
        payload = {**_VALID_PAYLOAD, "agent": "   "}
        status, body = await self._post(payload)
        assert status == 422

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_422(self):
        """client_resolver returning None means the agent isn't configured."""
        payload = {**_VALID_PAYLOAD, "agent": "unknown-bot"}
        status, body = await self._post(payload)
        assert status == 422
        assert any("unknown agent" in d for d in body["details"])


# ---------------------------------------------------------------------------
# Happy path (200)
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_draft_id_and_card_ts(self, tmp_path):
        """POST with valid payload returns 200 with draft_id and card_ts."""
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(return_value={"ts": "1111.0001"})
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

        store = MagicMock()
        store.create = MagicMock()

        packs_dir = Path(__file__).resolve().parents[3] / "packs"
        app, _, _ = _make_app(store=store, client_resolver=client_resolver, packs_dir=packs_dir)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert "draft_id" in body
            assert body["card_ts"] == "1111.0001"

    @pytest.mark.asyncio
    async def test_draft_persisted_to_store(self, tmp_path):
        """On success the Draft is written to the store exactly once."""
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(return_value={"ts": "2222.0001"})
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

        store = MagicMock()
        store.create = MagicMock()

        packs_dir = Path(__file__).resolve().parents[3] / "packs"
        app, _, _ = _make_app(store=store, client_resolver=client_resolver, packs_dir=packs_dir)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )

        store.create.assert_called_once()
        draft = store.create.call_args[0][0]
        assert draft.agent_name == "sam"
        assert draft.action_verb == "dispatch_issue"
        assert draft.capability_instance == "dispatch"
        assert "issue_url" in draft.payload
        assert draft.payload["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_issue_url_constructed_from_repo_and_issue(self, tmp_path):
        """draft.payload['issue_url'] is built from repo + issue number."""
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(return_value={"ts": "3333.0001"})
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

        store = MagicMock()
        store.create = MagicMock()

        packs_dir = Path(__file__).resolve().parents[3] / "packs"
        app, _, _ = _make_app(store=store, client_resolver=client_resolver, packs_dir=packs_dir)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )

        draft = store.create.call_args[0][0]
        assert draft.payload["issue_url"] == "https://github.com/bramveen1/ai-dev-team/issues/247"

    @pytest.mark.asyncio
    async def test_slack_postmessage_uses_correct_channel_and_thread(self, tmp_path):
        """Approval card is posted to the channel+thread_ts from payload."""
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(return_value={"ts": "4444.0001"})
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

        store = MagicMock()
        store.create = MagicMock()

        packs_dir = Path(__file__).resolve().parents[3] / "packs"
        app, _, _ = _make_app(store=store, client_resolver=client_resolver, packs_dir=packs_dir)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C001"
        assert call_kwargs["thread_ts"] == "1234567890.123456"


# ---------------------------------------------------------------------------
# 502 — Slack post fails, draft still persisted
# ---------------------------------------------------------------------------


class TestSlackPostFailure:
    @pytest.mark.asyncio
    async def test_502_when_slack_post_raises(self, tmp_path):
        """When Slack raises, endpoint returns 502 and draft is still stored."""
        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(side_effect=Exception("slack down"))
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

        store = MagicMock()
        store.create = MagicMock()

        packs_dir = Path(__file__).resolve().parents[3] / "packs"
        app, _, _ = _make_app(store=store, client_resolver=client_resolver, packs_dir=packs_dir)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/internal/drafts",
                json=_VALID_PAYLOAD,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            assert resp.status == 502
            body = await resp.json()
            assert body["error"] == "slack_post_failed"
            assert "draft_id" in body

        # Draft was still persisted even though Slack failed.
        store.create.assert_called_once()


# ---------------------------------------------------------------------------
# GET /internal/drafts
# ---------------------------------------------------------------------------


class TestListDrafts:
    @pytest.mark.asyncio
    async def test_returns_pending_drafts_for_agent(self):
        from datetime import datetime, timezone

        from router.approvals.store import Draft

        draft = Draft(
            draft_id="abc123",
            agent_name="sam",
            capability_type="pack",
            capability_instance="dispatch",
            action_verb="dispatch_issue",
            payload={"issue_url": "https://github.com/o/r/issues/1"},
            slack_channel="C001",
            slack_message_ts="9999.0001",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        store = MagicMock()
        store.list_by_status.return_value = [draft]
        app, _, _ = _make_app(store=store)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/internal/drafts?agent=sam&status=pending",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert len(body["drafts"]) == 1
            assert body["drafts"][0]["draft_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_filters_by_agent(self):
        from datetime import datetime, timezone

        from router.approvals.store import Draft

        def make_draft(agent, did):
            return Draft(
                draft_id=did,
                agent_name=agent,
                capability_type="pack",
                capability_instance="dispatch",
                action_verb="dispatch_issue",
                payload={},
                slack_channel="C001",
                slack_message_ts="1.0",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        store = MagicMock()
        store.list_by_status.return_value = [
            make_draft("sam", "d1"),
            make_draft("lisa", "d2"),
        ]
        app, _, _ = _make_app(store=store)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/internal/drafts?agent=sam",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            body = await resp.json()
            assert all(d["agent_name"] == "sam" for d in body["drafts"])

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self):
        app, _, _ = _make_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/internal/drafts")
            assert resp.status == 401


# ---------------------------------------------------------------------------
# check_token_configured
# ---------------------------------------------------------------------------


class TestCheckTokenConfigured:
    def test_raises_when_env_var_missing(self, monkeypatch):
        from router import internal_api

        monkeypatch.delenv("ROUTER_INTERNAL_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="ROUTER_INTERNAL_TOKEN"):
            internal_api.check_token_configured()

    def test_no_error_when_token_present(self, monkeypatch):
        from router import internal_api

        monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "some-token")
        internal_api.check_token_configured()  # must not raise


# ---------------------------------------------------------------------------
# Parallel-draft smoke: 5 drafts, no cross-contamination
# ---------------------------------------------------------------------------


class TestParallelDrafts:
    @pytest.mark.asyncio
    async def test_five_drafts_get_distinct_ids(self, tmp_path):
        """Five concurrent POST requests produce five distinct draft_ids."""
        import asyncio

        slack_client = AsyncMock()
        slack_client.chat_postMessage = AsyncMock(return_value={"ts": "5555.0001"})
        client_resolver = lambda name: slack_client if name == "sam" else None  # noqa: E731

        created_drafts = []

        store = MagicMock()
        store.create = MagicMock(side_effect=lambda d: created_drafts.append(d))

        packs_dir = Path(__file__).resolve().parents[3] / "packs"
        app, _, _ = _make_app(store=store, client_resolver=client_resolver, packs_dir=packs_dir)

        async with TestClient(TestServer(app)) as client:
            payloads = [{**_VALID_PAYLOAD, "issue": 100 + i} for i in range(5)]
            tasks = [
                client.post(
                    "/internal/drafts",
                    json=p,
                    headers={"Authorization": f"Bearer {_TOKEN}"},
                )
                for p in payloads
            ]
            responses = await asyncio.gather(*tasks)
            assert all(r.status == 200 for r in responses)
            bodies = [json.loads(await r.text()) for r in responses]

        draft_ids = [b["draft_id"] for b in bodies]
        assert len(set(draft_ids)) == 5, "expected 5 distinct draft_ids"
