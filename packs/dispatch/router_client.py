"""HTTP client for the router's compose-internal API (port 8090).

Owns the transport shared by the router-callback verbs in handler.py —
``dispatch_draft`` / ``dispatch_list_pending_drafts`` (POST/GET
``/internal/drafts``) and the #707 Discord status callback (POST
``/internal/status``): token resolution (env with secrets.json
fall-through), the request/error ladder, and the structured error-dict
mapping. handler.py re-exports the moved names, and its verbs keep
resolving ``_read_router_token`` through the handler module so existing
test patch targets hold.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

ROUTER_INTERNAL_URL = "http://router:8090/internal/drafts"
ROUTER_INTERNAL_STATUS_URL = "http://router:8090/internal/status"
ROUTER_INTERNAL_TOKEN_ENV = "ROUTER_INTERNAL_TOKEN"
DISPATCH_DRAFT_TIMEOUT_S = 10.0
DISPATCH_STATUS_TIMEOUT_S = 5.0


def read_router_token() -> str | None:
    """Read ROUTER_INTERNAL_TOKEN from env with secrets.json fall-through.

    Matches the dispatch_hook.py / workers_bot_token pattern so the secret
    can live in either the container env or ``data/secrets.json``.
    """
    try:
        from router.packs.secret_store import SecretStore  # noqa: PLC0415
    except ImportError:
        return os.environ.get(ROUTER_INTERNAL_TOKEN_ENV)

    return os.environ.get(ROUTER_INTERNAL_TOKEN_ENV) or SecretStore().get_str(ROUTER_INTERNAL_TOKEN_ENV.lower())


def handle_http_error_response(resp_body: bytes, http_code: int) -> dict[str, Any]:
    """Parse an error HTTP response body into a structured error dict."""
    try:
        body = json.loads(resp_body.decode())
    except Exception:
        body = {}
    if http_code == 401:
        return {"status": "error", "reason": "unauthorized", "detail": body.get("error", "401")}
    if http_code == 422:
        return {
            "status": "error",
            "reason": "validation_error",
            "detail": body,
        }
    if http_code == 502:
        return {
            "status": "error",
            "reason": "slack_post_failed",
            "draft_id": body.get("draft_id"),
            "detail": body.get("error", "slack_post_failed"),
        }
    return {"status": "error", "reason": f"http_{http_code}", "detail": body}


def call_router(
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
    _urlopen: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """One authenticated round-trip to the internal API.

    ``payload`` set → JSON POST; ``None`` → GET. Returns ``(body, None)``
    on success or ``(None, error_dict)`` on failure, where the error dict
    is exactly the shape the verbs have always returned (HTTPError →
    :func:`handle_http_error_response`, URLError → ``network_error``,
    anything else → ``unexpected_error`` — never claim success).
    """
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
        method = "POST"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)

    opener = _urlopen or urlrequest.urlopen
    try:
        resp = opener(req, timeout=timeout)
        return json.loads(resp.read().decode()), None
    except HTTPError as e:
        resp_body = e.read() if hasattr(e, "read") else b""
        return None, handle_http_error_response(resp_body, e.code)
    except URLError as e:
        detail = str(getattr(e, "reason", e))
        return None, {"status": "error", "reason": "network_error", "detail": detail}
    except Exception as e:
        return None, {"status": "error", "reason": "unexpected_error", "detail": str(e)}
