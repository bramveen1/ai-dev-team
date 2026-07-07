"""Config web page + REST API for runtime settings, secrets, and token files (#576).

Mounted on the existing health-check HTTP server (port 8080, published on
``127.0.0.1`` only — see docker-compose). Trust model matches ``/logs`` and
``/wakeup``: the compose network is internal and the host port is loopback-
bound, so the intended access path from a workstation is an SSH tunnel::

    ssh -L 8080:127.0.0.1:8080 <host>   →   http://localhost:8080/config

SSH is the authentication layer; anyone with shell on the host can already
edit ``.env`` directly, so the page grants no access shell users lack.

Endpoints
---------
GET    /config                        — the single-page UI (no build tooling)
GET    /config/api/settings           — registry + current values (secrets masked)
PUT    /config/api/settings/{key}     — validate + persist; body ``{"value": ...}``
DELETE /config/api/settings/{key}     — unset (env/default apply again)
GET    /config/api/tokenfiles         — list ``config/secrets/*.token`` (names only)
PUT    /config/api/tokenfiles/{name}  — create/replace; body ``{"content": "..."}``
DELETE /config/api/tokenfiles/{name}  — remove

Save-time channel validation (#576's coupled improvement, moved earlier):
when a ``channel``-typed setting is saved and a validator is wired in (see
:func:`set_channel_validator`), the ID is resolved against Slack *at save
time* — a typo'd ``AUTO_DISPATCH_CHANNEL`` is rejected in the UI instead of
failing ``channel_not_found`` every tick 30 minutes later. ``force=1``
bypasses the live check (format check still applies).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from router import agent_admin
from router import settings as settings_mod
from router.settings import REGISTRY, RuntimeSettings

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Token files live next to the other per-agent secrets. /config is the
# bind-mount inside the container; the repo path is the CI/dev fallback.
MOUNTED_SECRETS_DIR = Path("/config/secrets")

_TOKEN_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.token$")
_MAX_BODY_BYTES = 64 * 1024

# Optional async callable ``(channel_id) -> error message | None`` wired in by
# app.py once Slack clients exist. None → live validation silently skipped.
ChannelValidator = Callable[[str], Awaitable[str | None]]
_channel_validator: ChannelValidator | None = None


def set_channel_validator(fn: ChannelValidator | None) -> None:
    """Wire in the save-time Slack channel resolver (called from app startup)."""
    global _channel_validator
    _channel_validator = fn


def secrets_dir() -> Path:
    """Directory holding ``*.token`` files (mount when present, repo fallback)."""
    if MOUNTED_SECRETS_DIR.is_dir():
        return MOUNTED_SECRETS_DIR
    return REPO_ROOT / "config" / "secrets"


def _mask(value: str) -> str:
    """Mask a sensitive value, keeping the last 4 chars when long enough to be safe."""
    if not value:
        return ""
    if len(value) > 8:
        return "••••" + value[-4:]
    return "••••"


def _settings() -> RuntimeSettings:
    return settings_mod.get_settings()


async def _handle_get_settings(request: web.Request) -> web.Response:
    rs = _settings()
    items: list[dict[str, Any]] = []
    for entry in REGISTRY.values():
        value = rs.get(entry.key)
        is_set = rs.source(entry.key) != "default"
        display: Any = value
        if entry.is_sensitive:
            display = _mask(str(value or ""))
        items.append(
            {
                "key": entry.key,
                "kind": entry.kind,
                "type": entry.type,
                "category": entry.category,
                "description": entry.description,
                "reload": entry.reload,
                "choices": list(entry.choices),
                "default": entry.default,
                "editable": entry.kind != "boot",
                "sensitive": entry.is_sensitive,
                "set": is_set,
                "value": display,
                "source": rs.source(entry.key),
            }
        )
    # Per-agent credentials live in the Agents section (GET /config/api/agents)
    # — only genuinely global boot vars remain in this list.
    return web.json_response(
        {
            "settings": items,
            "runtime_path": str(rs.path),
            "secrets_dir": str(secrets_dir()),
        }
    )


async def _read_json_body(request: web.Request) -> dict[str, Any] | None:
    if request.content_length and request.content_length > _MAX_BODY_BYTES:
        return None
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _handle_put_setting(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    entry = REGISTRY.get(key)
    if entry is None:
        return web.json_response({"error": f"unknown setting {key!r}"}, status=404)

    body = await _read_json_body(request)
    if body is None or "value" not in body:
        return web.json_response({"error": "body must be JSON with a 'value' field"}, status=400)

    rs = _settings()
    raw = body["value"]

    # Live Slack resolution for channel-typed settings (skippable via force=1).
    force = request.rel_url.query.get("force") in ("1", "true")
    if entry.type == "channel" and isinstance(raw, str) and raw and _channel_validator is not None and not force:
        try:
            error = await _channel_validator(raw)
        except Exception as exc:
            logger.warning("config page: channel validation errored for %s: %s", key, exc)
            error = None  # validator outage must not block config edits
        if error:
            return web.json_response({"error": f"channel validation failed: {error}", "validation": True}, status=422)

    try:
        stored = rs.set(key, raw)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)

    logger.info("config page: %s updated (reload=%s)", key, entry.reload)
    display = _mask(str(stored)) if entry.is_sensitive else stored
    return web.json_response(
        {
            "ok": True,
            "key": key,
            "value": display,
            "source": rs.source(key),
            "applied": "hot" if entry.reload == "hot" else "restart-required",
        }
    )


async def _handle_delete_setting(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    entry = REGISTRY.get(key)
    if entry is None:
        return web.json_response({"error": f"unknown setting {key!r}"}, status=404)
    rs = _settings()
    try:
        existed = rs.unset(key)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)
    logger.info("config page: %s reset (existed=%s)", key, existed)
    return web.json_response({"ok": True, "key": key, "existed": existed, "source": rs.source(key)})


def _safe_token_path(name: str) -> Path | None:
    """Resolve a token-file name inside secrets_dir, rejecting traversal."""
    if not _TOKEN_FILE_RE.match(name) or ".." in name:
        return None
    return secrets_dir() / name


async def _handle_list_tokenfiles(request: web.Request) -> web.Response:
    base = secrets_dir()
    files = []
    if base.is_dir():
        for path in sorted(base.glob("*.token")):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({"name": path.name, "size": stat.st_size, "mtime": int(stat.st_mtime)})
    return web.json_response({"files": files, "dir": str(base)})


async def _handle_put_tokenfile(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = _safe_token_path(name)
    if path is None:
        return web.json_response({"error": "invalid token file name (want <name>.token)"}, status=400)
    body = await _read_json_body(request)
    if body is None or not isinstance(body.get("content"), str):
        return web.json_response({"error": "body must be JSON with a string 'content' field"}, status=400)
    content = body["content"].strip()
    if not content:
        return web.json_response({"error": "content must not be empty (DELETE to remove)"}, status=400)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    logger.info("config page: token file %s written (%d bytes)", name, len(content))
    return web.json_response({"ok": True, "name": name})


async def _handle_delete_tokenfile(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = _safe_token_path(name)
    if path is None:
        return web.json_response({"error": "invalid token file name"}, status=400)
    try:
        path.unlink()
    except FileNotFoundError:
        return web.json_response({"error": f"{name} not found"}, status=404)
    logger.info("config page: token file %s deleted", name)
    return web.json_response({"ok": True, "name": name})


async def _handle_page(request: web.Request) -> web.Response:
    html_path = STATIC_DIR / "config.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError:
        return web.Response(text="config UI asset missing (router/static/config.html)", status=500)
    return web.Response(text=html, content_type="text/html")


def register_routes(app: web.Application) -> None:
    """Mount the config page + API onto the health-check server's app."""
    app.router.add_get("/config", _handle_page)
    app.router.add_get("/config/api/settings", _handle_get_settings)
    app.router.add_put("/config/api/settings/{key}", _handle_put_setting)
    app.router.add_delete("/config/api/settings/{key}", _handle_delete_setting)
    app.router.add_get("/config/api/tokenfiles", _handle_list_tokenfiles)
    app.router.add_put("/config/api/tokenfiles/{name}", _handle_put_tokenfile)
    app.router.add_delete("/config/api/tokenfiles/{name}", _handle_delete_tokenfile)
    agent_admin.register_routes(app)
