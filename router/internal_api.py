"""Internal HTTP API for the structured dispatch approval flow.

Exposes ``POST /internal/drafts`` on port 8090 (compose-internal, NOT
host-mapped).  Agent containers call this endpoint to create approval drafts
without going through the Slack prose interceptor.

Auth: ``Authorization: Bearer $ROUTER_INTERNAL_TOKEN`` — shared secret
injected into router and every agent container at compose start.

``GET /internal/drafts?agent=<name>&status=pending`` returns the agent's
outstanding drafts so ``dispatch.list_pending_drafts`` can surface orphaned
cards for retry.

Payload schema for POST (all fields required, no extra fields):

    {
      "agent":           string,            # agent name (e.g. "sam")
      "repo":            "owner/repo",      # GitHub repo slug
      "issue":           integer > 0,       # issue number
      "title":           string,            # short description for the card
      "model":           "sonnet"|"opus"|"haiku",
      "persona":         "dev"|"review",
      "supervision_mode":"poll"|"stream",
      "budget_seconds":  integer > 0,
      "gate_reason":     string,            # why approval was required
      "thread_ts":       string,            # Slack thread timestamp
      "channel":         string             # Slack channel ID
    }

Responses:
    200  {draft_id, card_ts}
    400  invalid JSON
    401  missing/wrong/bad-scheme token
    422  {error: "validation_failed", details: [...]}
    502  {draft_id, error: "slack_post_failed"}  (draft persisted; card not posted)
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from router.approvals.block_kit import build_approval_message_from_specs
from router.approvals.button_resolver import resolve_buttons
from router.approvals.expiration_worker import get_ttl
from router.approvals.store import Draft, DraftStore
from router.packs.loader import discover_packs

logger = logging.getLogger(__name__)

INTERNAL_PORT = 8090

_VALID_MODELS = frozenset({"sonnet", "opus", "haiku"})
_VALID_PERSONAS = frozenset({"dev", "review"})
_VALID_SUPERVISION_MODES = frozenset({"poll", "stream"})

_REQUIRED_FIELDS = frozenset(
    {
        "agent",
        "repo",
        "issue",
        "title",
        "model",
        "persona",
        "supervision_mode",
        "budget_seconds",
        "gate_reason",
        "thread_ts",
        "channel",
    }
)


def check_token_configured() -> None:
    """Fail fast at router startup if ROUTER_INTERNAL_TOKEN is missing."""
    if not os.environ.get("ROUTER_INTERNAL_TOKEN"):
        raise RuntimeError(
            "ROUTER_INTERNAL_TOKEN is not set. "
            "Set it in data/secrets.json and redeploy so the token is "
            "injected into the router and every agent container."
        )


def _validate_bearer(request: web.Request, expected: str) -> bool:
    """Return True iff the request carries the correct Bearer token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer ") :] == expected


def _validate_payload(data: dict) -> list[str]:
    """Validate the POST /internal/drafts body.  Returns a list of error strings."""
    errors: list[str] = []

    unknown = set(data.keys()) - _REQUIRED_FIELDS
    if unknown:
        errors.append(f"unknown field(s): {', '.join(sorted(unknown))}")

    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"missing required field(s): {', '.join(sorted(missing))}")
        return errors

    if data["model"] not in _VALID_MODELS:
        errors.append(f"invalid model {data['model']!r}; must be one of {sorted(_VALID_MODELS)}")
    if data["persona"] not in _VALID_PERSONAS:
        errors.append(f"invalid persona {data['persona']!r}; must be one of {sorted(_VALID_PERSONAS)}")
    if data["supervision_mode"] not in _VALID_SUPERVISION_MODES:
        errors.append(
            f"invalid supervision_mode {data['supervision_mode']!r}; must be one of {sorted(_VALID_SUPERVISION_MODES)}"
        )
    if not isinstance(data["issue"], int) or data["issue"] <= 0:
        errors.append(f"issue must be a positive integer, got {data['issue']!r}")
    if not isinstance(data["budget_seconds"], (int, float)) or data["budget_seconds"] <= 0:
        errors.append(f"budget_seconds must be a positive number, got {data['budget_seconds']!r}")

    for field in ("agent", "repo", "title", "gate_reason", "thread_ts", "channel"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            errors.append(f"field {field!r} must be a non-empty string")

    return errors


async def _handle_create_draft(
    request: web.Request,
    *,
    token: str,
    store: DraftStore,
    client_resolver: Callable[[str], Any | None],
    packs_dir: Path | None,
    ttl_config: dict | None,
) -> web.Response:
    if not _validate_bearer(request, token):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "payload must be a JSON object"}, status=422)

    errors = _validate_payload(data)
    if errors:
        return web.json_response({"error": "validation_failed", "details": errors}, status=422)

    agent_name: str = data["agent"]
    client = client_resolver(agent_name)
    if client is None:
        return web.json_response(
            {"error": "validation_failed", "details": [f"unknown agent {agent_name!r}"]},
            status=422,
        )

    issue_url = f"https://github.com/{data['repo']}/issues/{data['issue']}"
    draft_payload: dict[str, Any] = {
        "issue_url": issue_url,
        "repo": data["repo"],
        "issue": data["issue"],
        "title": data["title"],
        "model": data["model"],
        "persona": data["persona"],
        "supervision_mode": data["supervision_mode"],
        "budget_seconds": int(data["budget_seconds"]),
        "gate_reason": data["gate_reason"],
    }

    packs = discover_packs(packs_dir)
    dispatch_pack = packs.get("dispatch")
    button_specs = resolve_buttons(action_verb="dispatch_issue", pack=dispatch_pack)

    now = datetime.now(timezone.utc)
    ttl = get_ttl("pack", ttl_config)
    draft_id = uuid.uuid4().hex
    draft = Draft(
        draft_id=draft_id,
        agent_name=agent_name,
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload=draft_payload,
        slack_channel=data["channel"],
        slack_message_ts="",
        draft_type="direct",
        created_at=now,
        expires_at=now + ttl,
    )

    approval_msg = build_approval_message_from_specs(draft, button_specs)

    try:
        result = await client.chat_postMessage(
            channel=data["channel"],
            thread_ts=data["thread_ts"],
            blocks=approval_msg["blocks"],
            text=f"{agent_name.capitalize()} wants to dispatch_issue",
        )
        card_ts: str = result["ts"]
        draft.slack_message_ts = card_ts
    except Exception as exc:
        # Draft persisted even when Slack post fails so list_pending_drafts
        # can surface it for retry (AC: Slack-post-fail path).
        store.create(draft)
        logger.error("slack_post_failed draft=%s agent=%s exc=%s", draft_id, agent_name, exc)
        return web.json_response({"draft_id": draft_id, "error": "slack_post_failed"}, status=502)

    store.create(draft)
    logger.info(
        "draft_created draft_id=%s agent=%s issue=%s model=%s card_ts=%s",
        draft_id,
        agent_name,
        issue_url,
        data["model"],
        card_ts,
    )
    return web.json_response({"draft_id": draft_id, "card_ts": card_ts})


async def _handle_list_drafts(
    request: web.Request,
    *,
    token: str,
    store: DraftStore,
) -> web.Response:
    if not _validate_bearer(request, token):
        return web.json_response({"error": "unauthorized"}, status=401)

    status = request.rel_url.query.get("status", "pending")
    agent = request.rel_url.query.get("agent")

    drafts = store.list_by_status(status)
    if agent:
        drafts = [d for d in drafts if d.agent_name == agent]

    return web.json_response(
        {
            "drafts": [
                {
                    "draft_id": d.draft_id,
                    "agent_name": d.agent_name,
                    "action_verb": d.action_verb,
                    "status": d.status,
                    "slack_channel": d.slack_channel,
                    "slack_message_ts": d.slack_message_ts,
                    "created_at": d.created_at.isoformat(),
                    "payload": d.payload,
                }
                for d in drafts
            ]
        }
    )


async def start_server(
    port: int,
    token: str,
    store: DraftStore,
    client_resolver: Callable[[str], Any | None],
    *,
    packs_dir: Path | None = None,
    ttl_config: dict | None = None,
) -> web.AppRunner:
    """Start the internal API aiohttp server. Returns the runner for cleanup."""
    app = web.Application()

    async def create_draft(req: web.Request) -> web.Response:
        return await _handle_create_draft(
            req,
            token=token,
            store=store,
            client_resolver=client_resolver,
            packs_dir=packs_dir,
            ttl_config=ttl_config,
        )

    async def list_drafts(req: web.Request) -> web.Response:
        return await _handle_list_drafts(req, token=token, store=store)

    app.router.add_post("/internal/drafts", create_draft)
    app.router.add_get("/internal/drafts", list_drafts)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("internal_api started port=%d", port)
    return runner
