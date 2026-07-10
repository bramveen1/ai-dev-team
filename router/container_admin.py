"""Containers admin API — per-container Restart button on the /config page (#710).

Endpoints (registered onto the health server's app by router/config_page.py;
same loopback-only / SSH-tunnel trust model as the rest of /config):

    GET  /config/api/containers                — live compose-project containers
    POST /config/api/containers/{name}/restart  — docker restart <name>

Design notes
------------
* **Source of truth is Docker, not a static list**: the container set comes
  from ``docker ps --filter label=com.docker.compose.project=...``
  (:func:`router.container_exec.list_project_containers`) — a new agent
  container shows up here with zero code change. The restart route
  validates ``{name}`` against that same live set (an allowlist) before
  shelling out, so it can't be used to restart anything off-project.
* **Router restarting itself is a special case.** ``docker restart router``
  SIGTERMs this very process — the handler can't await it normally, or the
  202 response never flushes before the process dies. Self-restart fires a
  detached, fire-and-forget restart (:func:`router.container_exec.restart_container_detached`)
  and returns 202 immediately; every other target awaits
  :func:`router.container_exec.restart_container` and surfaces its exit
  status.
* **No new permission boundary**: same trust model as the rest of /config
  (loopback port + SSH tunnel is the auth layer). Restart adds no
  capability the already-mounted Docker socket didn't already grant.
* **Default-off flag**: every restart action (including router
  self-restart) is gated behind ``CONFIG_CONTAINER_RESTART_ENABLED``
  (unset/false = disabled). Listing containers is unaffected — only the
  restart route refuses when the flag is off, with the resolved flag also
  surfaced on the list response so the UI can hide/disable the buttons.
"""

from __future__ import annotations

import logging

from aiohttp import web

from router import container_exec
from router import settings as settings_mod

logger = logging.getLogger(__name__)

_RESTART_DISABLED_MSG = "container restart is disabled (set CONFIG_CONTAINER_RESTART_ENABLED=true to enable)"


def _restart_enabled() -> bool:
    return bool(settings_mod.get("CONFIG_CONTAINER_RESTART_ENABLED"))


async def _handle_list_containers(request: web.Request) -> web.Response:
    try:
        names = await container_exec.list_project_containers()
    except container_exec.DispatchError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    self_name = await container_exec.own_container_name()
    containers = [{"name": name, "self": name == self_name} for name in names]
    return web.json_response({"containers": containers, "self": self_name, "restart_enabled": _restart_enabled()})


async def _handle_restart_container(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    enabled = _restart_enabled()
    if not enabled:
        return web.json_response({"error": _RESTART_DISABLED_MSG}, status=403)

    try:
        valid_names = await container_exec.list_project_containers()
    except container_exec.DispatchError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    if name not in valid_names:
        return web.json_response({"error": f"unknown container {name!r}"}, status=400)

    self_name = await container_exec.own_container_name()
    if name == self_name:
        await container_exec.restart_container_detached(name, enabled=enabled)
        logger.info("config page: self-restart (%s) fired, detached", name)
        return web.json_response({"ok": True, "name": name, "self_restart": True}, status=202)

    try:
        _stdout, stderr, returncode = await container_exec.restart_container(name, enabled=enabled)
    except container_exec.DispatchTimeoutError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    if returncode != 0:
        return web.json_response({"error": stderr.strip() or f"docker restart exited {returncode}"}, status=500)

    logger.info("config page: container %s restarted", name)
    return web.json_response({"ok": True, "name": name, "self_restart": False}, status=202)


def register_routes(app: web.Application) -> None:
    """Mount the containers admin API onto the health-check server's app."""
    app.router.add_get("/config/api/containers", _handle_list_containers)
    app.router.add_post("/config/api/containers/{name}/restart", _handle_restart_container)
