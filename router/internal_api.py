"""Internal HTTP API for dispatch routing.

Exposes two endpoints on port 8090 (compose-internal, never host-mapped):
  POST /internal/drafts  — create a draft and post an approval card to Slack
  GET  /internal/drafts  — list pending drafts for an agent (?agent=<name>)

Auth: ``Authorization: Bearer $ROUTER_INTERNAL_TOKEN`` on every request.
The token is required at router startup; missing → fail-fast (see
:func:`check_token_configured`).

Persist-before-post semantics: the Draft row is written to SQLite *before*
the Slack chat.postMessage call.  If Slack fails the draft survives and
``dispatch.list_pending_drafts`` can surface it for retry; the caller
receives a 502 with the persisted ``draft_id``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from router.approvals.block_kit import build_approval_message_from_specs
from router.approvals.button_resolver import resolve_buttons
from router.approvals.expiration_worker import get_ttl
from router.approvals.store import Draft, DraftStore
from router.packs.loader import discover_packs

logger = logging.getLogger(__name__)

INTERNAL_PORT = 8090
TOKEN_ENV = "ROUTER_INTERNAL_TOKEN"

VALID_MODELS = frozenset({"sonnet", "opus", "haiku"})
VALID_PERSONAS = frozenset({"dev", "review"})
VALID_SUPERVISION_MODES = frozenset({"poll", "stream"})

REQUIRED_FIELDS = frozenset(
    {
        "agent",
        "issue",
        "title",
        "model",
        "persona",
        "supervision_mode",
        "budget_seconds",
        "thread_ts",
        "channel",
    }
)
OPTIONAL_FIELDS = frozenset({"repo", "gate_reason"})
ALL_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS

# Module-level state — set by configure() from router/app.py at startup.
_store: DraftStore | None = None
_client_resolver: Any = None  # callable(agent_name: str) -> Slack client | None
_packs_dir: Any = None  # Path | None — passed to discover_packs()


def configure(
    store: DraftStore,
    client_resolver: Any,
    packs_dir: Any = None,
) -> None:
    """Wire up shared state.  Called from router/app.py once at startup."""
    global _store, _client_resolver, _packs_dir
    _store = store
    _client_resolver = client_resolver
    _packs_dir = packs_dir


def check_token_configured() -> None:
    """Fail-fast at router startup if ROUTER_INTERNAL_TOKEN is not set."""
    if not os.environ.get(TOKEN_ENV):
        raise SystemExit(
            f"FATAL: {TOKEN_ENV} is not set. "
            "The router will not start without a shared internal API token. "
            f"Export {TOKEN_ENV} in the router's environment before starting."
        )


# ── Auth helpers ─────────────────────────────────────────────────────────────


def _get_expected_token() -> str:
    return os.environ.get(TOKEN_ENV, "")


def _check_auth(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[len("Bearer ") :]
    expected = _get_expected_token()
    return bool(expected and provided == expected)


# ── Core card-posting logic ───────────────────────────────────────────────────


async def _build_and_post_card(
    *,
    draft: Draft,
    thread_ts: str,
    client: Any,
) -> str:
    """Build the Block Kit approval card and post it to Slack.

    Returns the Slack message timestamp on success.
    Raises on any Slack API / network error — caller handles the 502 path.
    """
    packs = discover_packs(_packs_dir)
    pack = packs.get("dispatch")

    button_specs = resolve_buttons(
        action_verb=draft.action_verb,
        pack=pack,
        target=None,
        deep_link_url=None,
    )

    approval_msg = build_approval_message_from_specs(draft, button_specs)

    result = await client.chat_postMessage(
        channel=draft.slack_channel,
        thread_ts=thread_ts,
        blocks=approval_msg["blocks"],
        text=f"{draft.agent_name.capitalize()} wants to {draft.action_verb}",
    )
    return result["ts"]


# ── Request handlers ──────────────────────────────────────────────────────────


async def _handle_create_draft(request: web.Request) -> web.Response:
    """POST /internal/drafts — validate payload, persist draft, post card."""
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "body_not_object"}, status=422)

    # Reject unknown fields.
    unknown = set(body.keys()) - ALL_FIELDS
    if unknown:
        return web.json_response(
            {"error": "unknown_fields", "fields": sorted(unknown)},
            status=422,
        )

    # Check required fields.
    missing = [f for f in sorted(REQUIRED_FIELDS) if f not in body]
    if missing:
        return web.json_response(
            {"error": "missing_fields", "fields": missing},
            status=422,
        )

    # Validate enum fields.
    model = body.get("model")
    if model not in VALID_MODELS:
        return web.json_response(
            {"error": "invalid_model", "model": model, "valid": sorted(VALID_MODELS)},
            status=422,
        )

    persona = body.get("persona")
    if persona not in VALID_PERSONAS:
        return web.json_response(
            {"error": "invalid_persona", "persona": persona, "valid": sorted(VALID_PERSONAS)},
            status=422,
        )

    supervision_mode = body.get("supervision_mode")
    if supervision_mode not in VALID_SUPERVISION_MODES:
        return web.json_response(
            {
                "error": "invalid_supervision_mode",
                "supervision_mode": supervision_mode,
                "valid": sorted(VALID_SUPERVISION_MODES),
            },
            status=422,
        )

    if _store is None or _client_resolver is None:
        return web.json_response({"error": "server_not_configured"}, status=503)

    agent_name = str(body["agent"])
    channel = str(body["channel"])
    thread_ts = str(body["thread_ts"])
    repo = str(body.get("repo") or "")
    issue = body["issue"]
    title = str(body.get("title") or "")
    gate_reason = str(body.get("gate_reason") or "")
    budget_seconds = int(body["budget_seconds"])

    client = _client_resolver(agent_name)
    if client is None:
        return web.json_response({"error": "unknown_agent", "agent": agent_name}, status=422)

    # Build the payload that _execute_approved_draft reads at click time.
    issue_url = f"https://github.com/{repo}/issues/{issue}" if repo else ""
    draft_payload: dict[str, Any] = {
        "issue_url": issue_url,
        "repo": repo,
        "issue": issue,
        "title": title,
        "model": model,
        "persona": persona,
        "supervision_mode": supervision_mode,
        "budget_seconds": budget_seconds,
        "branch_target": "main",
        "gate_reason": gate_reason,
    }

    now = datetime.now(timezone.utc)
    ttl = get_ttl("pack")
    expires_at = now + ttl
    draft_id = uuid.uuid4().hex[:8]

    # Persist before post — draft survives a Slack failure.
    draft = Draft(
        draft_id=draft_id,
        agent_name=agent_name,
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload=draft_payload,
        slack_channel=channel,
        slack_message_ts="",  # Updated after Slack post succeeds.
        draft_type="direct",
        external_id=None,
        created_at=now,
        expires_at=expires_at,
    )
    _store.create(draft)

    logger.info(
        "internal_api: created draft draft_id=%s agent=%s channel=%s",
        draft_id,
        agent_name,
        channel,
    )

    try:
        card_ts = await _build_and_post_card(
            draft=draft,
            thread_ts=thread_ts,
            client=client,
        )
        _store.update_message_ts(draft_id, card_ts)
        logger.info(
            "internal_api: posted card draft_id=%s card_ts=%s",
            draft_id,
            card_ts,
        )
        return web.json_response({"draft_id": draft_id, "card_ts": card_ts})
    except Exception:
        logger.exception(
            "internal_api: Slack post failed for draft_id=%s (draft persisted with empty ts)",
            draft_id,
        )
        return web.json_response(
            {"draft_id": draft_id, "error": "slack_post_failed"},
            status=502,
        )


async def _handle_list_drafts(request: web.Request) -> web.Response:
    """GET /internal/drafts?agent=<name> — list pending drafts for an agent."""
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    if _store is None:
        return web.json_response({"error": "server_not_configured"}, status=503)

    agent_name = request.rel_url.query.get("agent", "").strip()
    if not agent_name:
        return web.json_response({"error": "missing_query_param", "param": "agent"}, status=400)

    drafts = _store.list_pending_for_agent(agent_name)
    return web.json_response(
        {
            "agent": agent_name,
            "drafts": [
                {
                    "draft_id": d.draft_id,
                    "action_verb": d.action_verb,
                    "slack_channel": d.slack_channel,
                    "slack_message_ts": d.slack_message_ts,
                    "created_at": d.created_at.isoformat(),
                    "expires_at": d.expires_at.isoformat() if d.expires_at else None,
                    "payload": d.payload,
                }
                for d in drafts
            ],
        }
    )


# ── Server setup ──────────────────────────────────────────────────────────────


def build_internal_app() -> web.Application:
    """Build the aiohttp Application for the internal API."""
    app = web.Application()
    app.router.add_post("/internal/drafts", _handle_create_draft)
    app.router.add_get("/internal/drafts", _handle_list_drafts)
    return app


async def start_internal_server(port: int = INTERNAL_PORT) -> web.AppRunner:
    """Start the internal HTTP server on ``port`` and return its runner.

    Port 8090 is compose-internal only — it must NOT be mapped to the host.
    """
    app = build_internal_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Internal API server listening on 0.0.0.0:%d/internal/drafts", port)
    return runner
