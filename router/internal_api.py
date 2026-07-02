"""Internal HTTP API for dispatch routing.

Exposes two endpoints on port 8090 (compose-internal, never host-mapped):
  POST /internal/drafts  — create a draft and post an approval card to Slack
                           or Discord, depending on the draft's transport.
  GET  /internal/drafts  — list pending drafts for an agent (?agent=<name>)

Auth: ``Authorization: Bearer $ROUTER_INTERNAL_TOKEN`` on every request.
The token is required at router startup; missing → fail-fast (see
:func:`check_token_configured`).

Persist-before-post semantics: the Draft row is written to SQLite *before*
the chat.postMessage call.  If the post fails the draft survives and
``dispatch.list_pending_drafts`` can surface it for retry; the caller
receives a 502 with the persisted ``draft_id``.

Discord card posting (#680)
---------------------------
When the request body contains ``transport="discord"`` and a non-empty
``conversation_id``, the approval card is posted to the originating Discord
thread via the per-agent adapter bot token (resolved by the
``discord_token_resolver`` wired in at startup).  The per-agent adapter bot
is already a guild member so it never 403s; the separate ``WORKERS_DISCORD_TOKEN``
bot identity is not used for card posting.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from router.approvals.adapters.slack import SlackApprovalAdapter
from router.approvals.button_resolver import resolve_buttons
from router.approvals.card import ApprovalCard
from router.approvals.expiration_worker import get_ttl
from router.approvals.store import Draft, DraftStore
from router.packs.loader import discover_packs

logger = logging.getLogger(__name__)

_DISCORD_API_BASE = "https://discord.com/api/v10"

INTERNAL_PORT = 8090
TOKEN_ENV = "ROUTER_INTERNAL_TOKEN"

VALID_MODELS = frozenset({"sonnet", "opus", "haiku"})
VALID_PERSONAS = frozenset({"dev", "review"})
VALID_SUPERVISION_MODES = frozenset({"poll", "inline"})

REQUIRED_FIELDS = frozenset(
    {
        "agent",
        "title",
        "model",
        "persona",
        "supervision_mode",
        "budget_seconds",
        "thread_ts",
        "channel",
    }
)
# ``issue`` is optional in existing-PR mode (pr_url set); required otherwise.
# ``pr_url`` is optional in issue mode; required in existing-PR mode.
# ``transport``/``conversation_id`` are sent unconditionally by the dispatch
# pack since #664 (TransportRef). The router accepts them here so Slack
# dispatch is not rejected with 422 unknown_fields; end-to-end threading of
# the transport through the approve→execute path is tracked in #665.
OPTIONAL_FIELDS = frozenset({"repo", "gate_reason", "issue", "pr_url", "transport", "conversation_id"})
ALL_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS

# Module-level state — set by configure() from router/app.py at startup.
_store: DraftStore | None = None
_client_resolver: Any = None  # callable(agent_name: str) -> Slack client | None
_discord_token_resolver: Any = None  # callable(agent_name: str) -> str | None
_packs_dir: Any = None  # Path | None — passed to discover_packs()


def configure(
    store: DraftStore,
    client_resolver: Any,
    packs_dir: Any = None,
    discord_token_resolver: Any = None,
) -> None:
    """Wire up shared state.  Called from router/app.py once at startup.

    discord_token_resolver: optional callable ``(agent_name: str) -> str | None``
        that returns the per-agent Discord bot token.  When provided,
        Discord-origin drafts post their approval card to Discord instead of
        Slack.  Wired in by app.py using ``load_discord_credentials``.
    """
    global _store, _client_resolver, _discord_token_resolver, _packs_dir
    _store = store
    _client_resolver = client_resolver
    _discord_token_resolver = discord_token_resolver
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

    card = ApprovalCard(
        draft_id=draft.draft_id,
        agent_name=draft.agent_name,
        pack=pack.name if pack is not None else None,
        capability_type=draft.capability_type,
        capability_instance=draft.capability_instance,
        action_verb=draft.action_verb,
        summary="",
        payload=draft.payload,
        actions=button_specs,
        expires_at=draft.expires_at,
    )
    approval_msg = SlackApprovalAdapter().render_approval_card(card)

    result = await client.chat_postMessage(
        channel=draft.slack_channel,
        thread_ts=thread_ts,
        blocks=approval_msg["blocks"],
        text=f"{draft.agent_name.capitalize()} wants to {draft.action_verb}",
    )
    return result["ts"]


# ── Discord card posting (#680) ──────────────────────────────────────────────


async def _build_and_post_discord_card(
    *,
    draft: Draft,
    conversation_id: str,
    token: str,
) -> str | None:
    """Render an ApprovalCard and post it to a Discord thread/channel.

    Uses the per-agent adapter bot token (already a guild member) so posts
    never 403.  Returns the Discord message ID on success, ``None`` on any
    error — callers handle the 502 path.

    ``conversation_id`` format: ``"discord:<guild_id>:<channel_id>:<thread_id>"``
    (thread_id is "0" when the message is in the channel root).
    """
    from router.approvals.adapters.discord import DiscordApprovalAdapter

    packs = discover_packs(_packs_dir)
    pack = packs.get("dispatch")
    button_specs = resolve_buttons(
        action_verb=draft.action_verb,
        pack=pack,
        target=None,
        deep_link_url=None,
    )
    card = ApprovalCard(
        draft_id=draft.draft_id,
        agent_name=draft.agent_name,
        pack=pack.name if pack is not None else None,
        capability_type=draft.capability_type,
        capability_instance=draft.capability_instance,
        action_verb=draft.action_verb,
        summary="",
        payload=draft.payload,
        actions=button_specs,
        expires_at=draft.expires_at,
    )
    content = DiscordApprovalAdapter().render_approval_card(card)["content"]

    try:
        body = conversation_id.removeprefix("discord:")
        parts = body.split(":")
        if len(parts) != 3:  # noqa: PLR2004
            raise ValueError("wrong number of parts")
        _guild_id, channel_id, thread_id = parts
    except (ValueError, AttributeError):
        logger.warning(
            "internal_api: malformed Discord conversation_id %r draft=%s",
            conversation_id,
            draft.draft_id,
        )
        return None

    target = thread_id if thread_id != "0" else channel_id
    url = f"{_DISCORD_API_BASE}/channels/{target}/messages"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=json.dumps({"content": content}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bot {token}",
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.warning(
                        "internal_api: Discord card post HTTP %d draft=%s: %s",
                        resp.status,
                        draft.draft_id,
                        text[:200],
                    )
                    return None
                data = await resp.json()
                return str(data.get("id") or "discord_posted")
    except Exception:
        logger.exception(
            "internal_api: Discord card post failed draft=%s conversation=%s",
            draft.draft_id,
            conversation_id,
        )
        return None


# ── Direct-call draft creation (used by router code, not HTTP) ────────────────


async def create_dispatch_draft(
    *,
    agent_name: str,
    channel: str,
    thread_ts: str,
    issue_url: str,
    issue_num: int | None,
    issue_title: str,
    model: str,
    persona: str,
    budget_seconds: int,
    gate_preview: dict,
) -> str:
    """Create a dispatch_issue draft and post the approval card to Slack.

    Called by ``auto_dispatch._dispatch_worker`` when the handler returns
    ``approval_required`` (i.e. the gate fired without ``--approved``).  This
    is the router-internal equivalent of the ``POST /internal/drafts`` HTTP
    endpoint: same persist-before-post semantics, same Draft schema, same
    Block Kit card — but avoids the HTTP round-trip so the auto path can
    create drafts without needing ``ROUTER_INTERNAL_TOKEN`` from inside the
    router process.

    Returns the ``draft_id`` of the created draft.  Raises if the store or
    client resolver has not been configured (``configure()`` not called).
    """
    if _store is None or _client_resolver is None:
        raise RuntimeError("internal_api not configured; call configure() first")

    repo = gate_preview.get("repo") or ""
    gate_reason = gate_preview.get("gate_reason") or ""

    draft_payload: dict[str, Any] = {
        "issue_url": issue_url,
        "repo": repo,
        "title": issue_title,
        "model": model,
        "persona": persona,
        "supervision_mode": "poll",
        "budget_seconds": budget_seconds,
        "branch_target": "main",
        "gate_reason": gate_reason,
    }
    if issue_num is not None:
        draft_payload["issue"] = issue_num

    now = datetime.now(timezone.utc)
    ttl = get_ttl("pack")
    expires_at = now + ttl
    draft_id = uuid.uuid4().hex[:8]

    draft = Draft(
        draft_id=draft_id,
        agent_name=agent_name,
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload=draft_payload,
        slack_channel=channel,
        slack_message_ts="",
        draft_type="direct",
        external_id=None,
        created_at=now,
        expires_at=expires_at,
    )
    _store.create(draft)

    logger.info(
        "internal_api: auto-path created draft draft_id=%s agent=%s channel=%s gate_reason=%s",
        draft_id,
        agent_name,
        channel,
        gate_reason,
    )

    client = _client_resolver(agent_name)
    if client is not None:
        try:
            card_ts = await _build_and_post_card(draft=draft, thread_ts=thread_ts, client=client)
            _store.update_message_ts(draft_id, card_ts)
            logger.info("internal_api: posted auto-path approval card draft_id=%s card_ts=%s", draft_id, card_ts)
        except Exception:
            logger.exception("internal_api: Slack post failed for auto-path draft_id=%s (draft persisted)", draft_id)
    else:
        logger.warning(
            "internal_api: no Slack client for agent=%s; card not posted for draft_id=%s",
            agent_name,
            draft_id,
        )

    return draft_id


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

    # Validate: must have either `issue` (issue mode) or `pr_url` (existing-PR mode).
    has_issue = "issue" in body
    has_pr_url = bool(body.get("pr_url"))
    if not has_issue and not has_pr_url:
        return web.json_response(
            {"error": "missing_fields", "fields": ["issue or pr_url required"]},
            status=422,
        )

    if _store is None or _client_resolver is None:
        return web.json_response({"error": "server_not_configured"}, status=503)

    agent_name = str(body["agent"])
    channel = str(body["channel"])
    thread_ts = str(body["thread_ts"])
    repo = str(body.get("repo") or "")
    issue = body.get("issue")
    pr_url = str(body.get("pr_url") or "")
    title = str(body.get("title") or "")
    gate_reason = str(body.get("gate_reason") or "")
    budget_seconds = int(body["budget_seconds"])
    transport = str(body.get("transport") or "")
    conversation_id = str(body.get("conversation_id") or "")

    # Route: Discord-origin drafts post their approval card to Discord;
    # all other drafts use the Slack path.
    is_discord = transport == "discord" and bool(conversation_id)

    if is_discord:
        # Discord path: validate via discord_token_resolver instead of Slack client.
        discord_token = _discord_token_resolver(agent_name) if _discord_token_resolver is not None else None
        if not discord_token:
            return web.json_response(
                {"error": "discord_agent_not_configured", "agent": agent_name},
                status=422,
            )
        client = None
    else:
        # Slack path: require a Slack client for this agent.
        client = _client_resolver(agent_name)
        if client is None:
            return web.json_response({"error": "unknown_agent", "agent": agent_name}, status=422)
        discord_token = None

    # Build the payload that _execute_approved_draft reads at click time.
    issue_url = f"https://github.com/{repo}/issues/{issue}" if (repo and issue is not None) else ""
    draft_payload: dict[str, Any] = {
        "issue_url": issue_url,
        "repo": repo,
        "title": title,
        "model": model,
        "persona": persona,
        "supervision_mode": supervision_mode,
        "budget_seconds": budget_seconds,
        "branch_target": "main",
        "gate_reason": gate_reason,
    }
    if issue is not None:
        draft_payload["issue"] = issue
    if pr_url:
        draft_payload["pr_url"] = pr_url
    if transport:
        draft_payload["transport"] = transport
    if conversation_id:
        draft_payload["conversation_id"] = conversation_id

    now = datetime.now(timezone.utc)
    ttl = get_ttl("pack")
    expires_at = now + ttl
    draft_id = uuid.uuid4().hex[:8]

    # Persist before post — draft survives a post failure.
    draft = Draft(
        draft_id=draft_id,
        agent_name=agent_name,
        capability_type="pack",
        capability_instance="dispatch",
        action_verb="dispatch_issue",
        payload=draft_payload,
        slack_channel=channel,
        slack_message_ts="",  # Updated after post succeeds.
        draft_type="direct",
        external_id=None,
        created_at=now,
        expires_at=expires_at,
    )
    _store.create(draft)

    logger.info(
        "internal_api: created draft draft_id=%s agent=%s transport=%s",
        draft_id,
        agent_name,
        transport or "slack",
    )

    if is_discord:
        # Post approval card to the originating Discord thread.
        card_ref = await _build_and_post_discord_card(
            draft=draft,
            conversation_id=conversation_id,
            token=discord_token,  # type: ignore[arg-type]
        )
        if card_ref:
            _store.update_message_ts(draft_id, card_ref)
            logger.info(
                "internal_api: posted Discord card draft_id=%s msg_id=%s",
                draft_id,
                card_ref,
            )
            return web.json_response({"draft_id": draft_id, "card_ref": card_ref})
        logger.warning(
            "internal_api: Discord card post failed for draft_id=%s (draft persisted)",
            draft_id,
        )
        return web.json_response(
            {"draft_id": draft_id, "error": "discord_post_failed"},
            status=502,
        )

    # Slack path.
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
