"""Internal HTTP endpoint for the router.

Exposes ``POST /internal/drafts`` on port 8090 (compose-internal only,
no host mapping). Accepts a validated dispatch-draft payload, writes
to the approvals store, posts the Slack approval card, and returns
``{draft_id, card_ts}``.

Bearer-token authenticated via ``ROUTER_INTERNAL_TOKEN`` env var.
The token **must** be present before the router starts; the process
exits if it is missing (no silent insecure mode).

Port 8090 is never mapped to the host in ``docker-compose.yml`` —
it is reachable only by other services inside the Compose network.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Callable

from aiohttp import web

from router.approvals.block_kit import ACTION_APPROVE_SEND, ACTION_DISCARD, build_approval_message
from router.approvals.store import Draft, DraftStore

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8090

_VALID_MODELS: frozenset[str] = frozenset({"sonnet", "opus", "haiku"})
_VALID_PERSONAS: frozenset[str] = frozenset({"dev", "review"})
_VALID_SUPERVISION_MODES: frozenset[str] = frozenset({"poll", "stream"})

_REQUIRED_FIELDS: frozenset[str] = frozenset(
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

# Type alias for the Slack client resolver: agent_name → WebClient | None
SlackClientResolver = Callable[[str], Any]


def check_token_configured() -> None:
    """Raise RuntimeError if ROUTER_INTERNAL_TOKEN is absent from the environment.

    Called at router startup so the process fails fast with a clear error
    rather than running in an unauthenticated state.
    """
    if not os.environ.get("ROUTER_INTERNAL_TOKEN"):
        raise RuntimeError(
            "ROUTER_INTERNAL_TOKEN is not set. Add this env var to docker-compose.yml (router service) and restart."
        )


def _validate_payload(data: Any) -> tuple[dict | None, str | None]:
    """Validate the dispatch-draft payload.

    Returns ``(validated_data, None)`` on success or ``(None, error_message)``
    on the first validation failure.  Rejects unknown fields and invalid enums.
    """
    if not isinstance(data, dict):
        return None, "body must be a JSON object"

    extra = set(data.keys()) - _REQUIRED_FIELDS
    if extra:
        return None, f"unknown field(s): {sorted(extra)}"

    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        return None, f"missing required field(s): {sorted(missing)}"

    if data["model"] not in _VALID_MODELS:
        return None, f"invalid model {data['model']!r}; must be one of {sorted(_VALID_MODELS)}"

    if data["persona"] not in _VALID_PERSONAS:
        return None, f"invalid persona {data['persona']!r}; must be one of {sorted(_VALID_PERSONAS)}"

    if data["supervision_mode"] not in _VALID_SUPERVISION_MODES:
        return None, (
            f"invalid supervision_mode {data['supervision_mode']!r}; must be one of {sorted(_VALID_SUPERVISION_MODES)}"
        )

    if not isinstance(data["issue"], int):
        return None, "field 'issue' must be an integer"

    if not isinstance(data["budget_seconds"], int):
        return None, "field 'budget_seconds' must be an integer"

    return data, None


def _make_handler(store: DraftStore, client_resolver: SlackClientResolver) -> Any:
    """Return the aiohttp request handler for ``POST /internal/drafts``."""

    async def handle_post_drafts(request: web.Request) -> web.Response:
        # --- Bearer auth ---
        expected_token = os.environ.get("ROUTER_INTERNAL_TOKEN", "")
        auth_header = request.headers.get("Authorization", "")
        provided = auth_header[len("Bearer ") :] if auth_header.startswith("Bearer ") else ""
        if not provided or provided != expected_token:
            return web.json_response({"error": "unauthorized"}, status=401)

        # --- Parse JSON body ---
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=422)

        # --- Validate payload ---
        validated, error = _validate_payload(data)
        if error:
            return web.json_response({"error": error}, status=422)

        assert validated is not None  # narrowed by _validate_payload above

        agent_name: str = validated["agent"]
        draft_id = str(uuid.uuid4())

        # Build the Draft (slack_message_ts filled in after the Slack post).
        draft = Draft(
            draft_id=draft_id,
            agent_name=agent_name,
            capability_type="dispatch",
            capability_instance=f"{validated['repo']}#{validated['issue']}",
            action_verb="dispatch",
            payload=validated,
            slack_channel=validated["channel"],
            slack_message_ts="",
        )

        approval_msg = build_approval_message(draft, buttons=[ACTION_APPROVE_SEND, ACTION_DISCARD])
        client = client_resolver(agent_name)

        # --- Post Slack card ---
        try:
            if client is None:
                raise RuntimeError(f"no Slack client available for agent {agent_name!r}")
            result = await client.chat_postMessage(
                channel=validated["channel"],
                thread_ts=validated["thread_ts"],
                blocks=approval_msg["blocks"],
                text=f"{agent_name.capitalize()} wants to dispatch: {validated['title']}",
            )
            card_ts: str = result["ts"]
        except Exception:
            logger.exception("Slack post failed for dispatch draft %s", draft_id)
            # Persist the draft even on Slack failure so the caller can retry
            # posting without re-creating the record (the draft_id is returned).
            store.create(draft)
            return web.json_response(
                {"draft_id": draft_id, "error": "slack_post_failed"},
                status=502,
            )

        draft.slack_message_ts = card_ts
        store.create(draft)

        logger.info(
            "Posted dispatch approval card draft=%s agent=%s repo=%s issue=%s",
            draft_id,
            agent_name,
            validated["repo"],
            validated["issue"],
        )

        return web.json_response({"draft_id": draft_id, "card_ts": card_ts})

    return handle_post_drafts


def build_app(store: DraftStore, client_resolver: SlackClientResolver) -> web.Application:
    """Build the aiohttp Application exposing ``POST /internal/drafts``."""
    app = web.Application()
    app.router.add_post("/internal/drafts", _make_handler(store, client_resolver))
    return app


async def start_server(
    store: DraftStore,
    client_resolver: SlackClientResolver,
    port: int = DEFAULT_PORT,
) -> web.AppRunner:
    """Start the internal API server and return its runner.

    Calls :func:`check_token_configured` first — raises immediately if
    ``ROUTER_INTERNAL_TOKEN`` is absent.  The caller keeps a reference to
    the returned runner and calls ``runner.cleanup()`` on shutdown.
    """
    check_token_configured()
    app = build_app(store, client_resolver)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Internal API server listening on 0.0.0.0:%d/internal/drafts", port)
    return runner
