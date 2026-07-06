"""Health-check HTTP endpoint for the router.

Exposes ``GET /healthz`` on a dedicated aiohttp web server so the
pull-based deploy daemon (``scripts/deploy-pull.sh``) can probe the
container from the host after each deploy.

Readiness model
---------------
The endpoint returns 200 only after :func:`mark_ready` has been called
*and* at least one Slack bot token is present in the environment. Both
conditions are required by the CD spec in issue #107:

* Slack token loaded — guards against shipping a build whose env file
  was wiped or rotated mid-flight.
* Event loop reached "ready" — guards against the case where the
  process is up but Socket Mode hasn't finished initializing.

The probe is intentionally local-only: it queries no external services
and returns within milliseconds. See ``docs/cd-deployment.md``.

``/logs`` endpoint (issue #218)
--------------------------------
``GET /logs?tail=N&max_bytes=M`` serves the most-recent N log lines
from the in-memory :mod:`router.log_buffer` ring buffer.  Lines have
already been redacted by the buffer before storage.  Query parameters:

- ``tail`` — number of lines to return (default 200, hard max 500)
- ``max_bytes`` — byte cap on the total response body (default 50 000,
  hard max 200 000)

The endpoint is intentionally unauthenticated within the Docker network
because only agents that hold the ``ops-diag`` pack can invoke it (the
pack's handler.py calls this URL), and the docker-compose network is
already internal-only.

``/wakeup`` endpoint (issue #323)
----------------------------------
``POST /wakeup`` lets discovered agents schedule a one-shot self-wakeup
without direct filesystem access to the router's ``data/`` volume. Trust
model: Docker-network-scoped, no host port binding — same as the
``/logs`` endpoint above.

Request body (JSON):
    agent         — must be a discovered agent id (config/agents/*/agent.yaml)
    delay_seconds — clamped to [60, 86400]
    reason        — free-text reason posted into the thread at fire time
    channel       — Slack channel ID
    thread_ts     — Slack thread timestamp

Response 200: ``{"task_id": "<uuid>", "fire_at": "<iso8601>"}``
Errors: 400 (bad payload / missing thread), 404 (unknown agent),
        503 (store unavailable or DB locked)
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from aiohttp import web

from router import log_buffer as _lb

if TYPE_CHECKING:
    from router.scheduled_tasks.store import ScheduledTaskStore

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080

_WAKEUP_DELAY_MIN = 60
_WAKEUP_DELAY_MAX = 86400

# Pattern for resolving ${SECRET:NAME} references — matches config.py's _SECRET_REF_RE.
_SECRET_REF_RE = re.compile(r"^\$\{SECRET:([A-Z0-9_]+)\}$")

_ready: bool = False
_wakeup_store: ScheduledTaskStore | None = None


def set_wakeup_store(store: ScheduledTaskStore) -> None:
    """Inject the scheduled-tasks store for the /wakeup endpoint. Call once after open_store()."""
    global _wakeup_store
    _wakeup_store = store


def reset_wakeup_store_for_tests() -> None:
    """Test-only hook to clear the injected store between cases."""
    global _wakeup_store
    _wakeup_store = None


def mark_ready() -> None:
    """Flip the readiness flag. Called once Socket Mode handlers are up."""
    global _ready
    _ready = True
    logger.info("Router readiness flag set; /healthz will now return 200")


def reset_ready_for_tests() -> None:
    """Test-only hook to reset the readiness flag between cases."""
    global _ready
    _ready = False


def workspace_volume_writable(workspace_root: str | None = None) -> bool:
    """Return True if the dispatch workspace root is present and writable.

    Tries to create a sentinel file under the workspace root to verify the
    mount is both present and owned by the running uid.  Absence means the
    bind-mount source was never provisioned on the host (the #691 footgun).
    Returns False without raising so callers can decide the severity.

    The root is resolved through :func:`router.dispatch.state.dispatch_root`
    so the ``DISPATCH_WORKSPACE_ROOT`` env var and its ``/var/lib/dispatch``
    default live in exactly one place (governance: no direct env reads outside
    the settings registry).
    """
    from router.dispatch.state import dispatch_root

    root = str(dispatch_root(workspace_root))
    if not os.path.isdir(root):
        return False
    try:
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".writable_probe_")
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


def _bot_token_names_from_agents(agent_map: dict[str, dict] | None = None) -> tuple[str, ...]:
    """Derive expected bot-token env var names from the configured agent map.

    For each discovered agent:
    - If ``backends.slack.bot_token`` is a ``${SECRET:X}`` reference, use ``X``.
    - Otherwise fall back to the ``<AGENT_UPPER>_BOT_TOKEN`` legacy convention.
    """
    from router import config as _cfg

    if agent_map is None:
        agent_map = _cfg.get_agent_map()
    names: list[str] = []
    for agent_id, agent_cfg in agent_map.items():
        backends = agent_cfg.get("backends") or {}
        slack_cfg = backends.get("slack") or {}
        token_ref = slack_cfg.get("bot_token") or ""
        m = _SECRET_REF_RE.match(token_ref)
        if m:
            names.append(m.group(1))
        else:
            names.append(f"{agent_id.upper()}_BOT_TOKEN")
    return tuple(names)


def _store_has_complete_slack_block(agent_map: dict[str, dict] | None = None) -> bool:
    """True when the secret store holds a complete Slack block for any discovered agent.

    Readiness must recognise store-configured deployments (credentials written
    via the /config page, no .env entries) or a store-only deployment would
    flap 503 forever and every deploy would read as failed.
    """
    from router import config as _cfg  # noqa: PLC0415 — deferred to avoid import cycle

    if agent_map is None:
        agent_map = _cfg.get_agent_map()
    for agent_id in agent_map:
        stored = _cfg.agent_store_credentials(agent_id, "slack")
        if all(isinstance(stored.get(f), str) and stored.get(f) for f in _cfg._SLACK_REQUIRED_FIELDS):
            return True
    return False


def _slack_token_present(env: dict[str, str] | None = None, *, names: Iterable[str] | None = None) -> bool:
    """Return True if Slack credentials exist for at least one agent.

    Two probes: (1) a non-empty bot-token env var (names derived from the
    configured agent map unless ``names`` is passed — tests do this to avoid
    filesystem lookups); (2) a complete per-agent Slack block in the secret
    store (data/secrets.json, managed via the /config page).
    """
    src = env if env is not None else os.environ
    if names is None:
        names = _bot_token_names_from_agents()
    if any(bool(src.get(name)) for name in names):
        return True
    return _store_has_complete_slack_block()


async def _handle_healthz(request: web.Request) -> web.Response:
    if _ready and _slack_token_present():
        return web.json_response({"status": "ok"})
    reason = "not ready" if not _ready else "slack token missing"
    return web.json_response({"status": reason}, status=503)


def _parse_int_param(request: web.Request, name: str, default: int, *, lo: int = 1, hi: int) -> int:
    raw = request.rel_url.query.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, val))


async def _handle_logs(request: web.Request) -> web.Response:
    """Serve recent router log lines from the in-memory ring buffer."""
    tail = _parse_int_param(
        request,
        "tail",
        _lb.DEFAULT_TAIL_LINES,
        hi=_lb.MAX_TAIL_LINES,
    )
    max_bytes = _parse_int_param(
        request,
        "max_bytes",
        _lb.DEFAULT_MAX_BYTES,
        hi=_lb.MAX_BYTES,
    )
    buf = _lb.get_buffer()
    if buf is None:
        return web.json_response(
            {"status": "unavailable", "lines": [], "line_count": 0},
            status=503,
        )
    lines = buf.get_recent(tail=tail, max_bytes=max_bytes)
    return web.json_response(
        {"status": "ok", "lines": lines, "line_count": len(lines)},
    )


async def _handle_wakeup(request: web.Request) -> web.Response:
    """Schedule a one-shot self-wakeup for a named agent (#323)."""
    if _wakeup_store is None:
        return web.json_response({"error": "store unavailable"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    from router.config import known_agent_ids  # noqa: PLC0415 — deferred to avoid import cycle

    agent = (body.get("agent") or "").strip()
    if agent not in known_agent_ids():
        return web.json_response({"error": "unknown agent", "agent": agent}, status=404)

    channel = (body.get("channel") or "").strip()
    thread_ts = (body.get("thread_ts") or "").strip()
    missing = [f for f, v in (("channel", channel), ("thread_ts", thread_ts)) if not v]
    if missing:
        return web.json_response({"error": "missing required fields", "missing": missing}, status=400)

    try:
        raw_delay = int(body.get("delay_seconds", _WAKEUP_DELAY_MIN))
    except (ValueError, TypeError):
        raw_delay = _WAKEUP_DELAY_MIN
    delay = max(_WAKEUP_DELAY_MIN, min(_WAKEUP_DELAY_MAX, raw_delay))

    reason = str(body.get("reason") or "").strip()

    fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    try:
        task = _wakeup_store.create_one_shot_task(
            agent_name=agent,
            next_run_at=fire_at,
            payload={"reason": reason, "channel_id": channel, "thread_ts": thread_ts},
            name="wakeup",
            destination=channel,
        )
    except Exception as exc:
        logger.warning("wakeup store error for agent=%s: %s", agent, exc)
        return web.json_response({"error": "store unavailable"}, status=503)

    return web.json_response({"task_id": task.task_id, "fire_at": fire_at.isoformat()})


def build_app() -> web.Application:
    """Build the aiohttp ``Application`` exposing ``/healthz``, ``/logs``, ``/wakeup``,
    and the runtime-config page/API under ``/config`` (#576 — see router/config_page.py)."""
    from router import config_page  # noqa: PLC0415 — deferred to avoid import cycle

    app = web.Application()
    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_get("/logs", _handle_logs)
    app.router.add_post("/wakeup", _handle_wakeup)
    config_page.register_routes(app)
    return app


async def start_server(port: int = DEFAULT_PORT) -> web.AppRunner:
    """Start the health-check HTTP server on ``port`` and return its runner.

    The caller is responsible for keeping a reference to the runner so it
    isn't garbage-collected. ``cleanup()`` on the returned runner stops
    the server cleanly during shutdown.
    """
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Health-check server listening on 0.0.0.0:%d/healthz", port)
    return runner
