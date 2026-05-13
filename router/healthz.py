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
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080

# Bot-token env vars we accept as evidence that "the Slack token is
# loaded". Any single match is enough — production deployments have
# many agents and we don't want to fail the probe just because one
# agent's bot token was rotated.
_SLACK_TOKEN_ENV_VARS: tuple[str, ...] = (
    "LISA_BOT_TOKEN",
    "SAM_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
)

_ready: bool = False


def mark_ready() -> None:
    """Flip the readiness flag. Called once Socket Mode handlers are up."""
    global _ready
    _ready = True
    logger.info("Router readiness flag set; /healthz will now return 200")


def reset_ready_for_tests() -> None:
    """Test-only hook to reset the readiness flag between cases."""
    global _ready
    _ready = False


def _slack_token_present(env: dict[str, str] | None = None, *, names: Iterable[str] = _SLACK_TOKEN_ENV_VARS) -> bool:
    """Return True if at least one Slack bot token env var is set & non-empty."""
    src = env if env is not None else os.environ
    return any(bool(src.get(name)) for name in names)


async def _handle_healthz(request: web.Request) -> web.Response:
    if _ready and _slack_token_present():
        return web.json_response({"status": "ok"})
    reason = "not ready" if not _ready else "slack token missing"
    return web.json_response({"status": reason}, status=503)


def build_app() -> web.Application:
    """Build the aiohttp ``Application`` exposing ``/healthz``."""
    app = web.Application()
    app.router.add_get("/healthz", _handle_healthz)
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
