"""Shared helpers for the /config web layer (config_page, agent_admin).

Owns the request/response plumbing both modules used to duplicate
(refactoring roadmap §3): bounded JSON-body parsing and sensitive-value
masking. Body-size caps stay per-module — the settings API accepts small
payloads while agent creation carries role/personality text.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web


def mask(value: str) -> str:
    """Mask a sensitive value, keeping the last 4 chars when long enough to be safe."""
    if not value:
        return ""
    if len(value) > 8:
        return "••••" + value[-4:]
    return "••••"


async def read_json_body(request: web.Request, *, max_bytes: int) -> dict[str, Any] | None:
    """Parse a JSON object body; ``None`` on oversize, invalid JSON, or non-dict."""
    if request.content_length and request.content_length > max_bytes:
        return None
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None
