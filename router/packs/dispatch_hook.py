"""Dispatch-time hook — turn an agent's ``packs:`` list into CLI flags.

The dispatcher calls :func:`pack_cli_extras` once per dispatch. The function
reads the agent's manifest, looks up each declared pack, and returns:

- a list of additional ``--append-system-prompt-file`` paths (one per pack
  that ships a ``prompt.md``)
- a generated ``.mcp.json`` path (or ``None`` if no pack ships an
  ``mcp.json``); the dispatcher passes this with ``--mcp-config``
- a list of env vars (``KEY=VALUE`` strings) sourced from the secret store
  for every secret listed in any pack's ``needs:``

When an agent has no ``packs:`` key, this returns empty results — the
dispatcher's behavior is unchanged. That's the additive guarantee for PR 2.

Pack files live on the host. Inside the agent container they are visible at
``/config/packs/<name>/`` because we mount ``./packs`` read-only in
``scripts/render_compose.py``.
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from router.packs.loader import Pack, discover_packs
from router.packs.secret_store import SecretStore

logger = logging.getLogger(__name__)

CONTAINER_PACKS_DIR = "/config/packs"
# Read agent manifests from the live ``./config`` bind-mount (``/config``
# inside the router container), NOT from ``/app/config`` which is baked
# into the router image at build time and goes stale after any edit to a
# gitignored ``agent.yaml``. See PR for the full drift writeup.
DEFAULT_MANIFEST_PATH_TEMPLATE = Path("/config/agents/{agent}/agent.yaml")

# Pack name that opts an agent into having Slack context (channel,
# thread_ts, agent name) injected into its env at dispatch time so the
# pack's ``handler.py`` can dispatch follow-up issues without explicit
# CLI flags. Mirrors the GITHUB_TOKEN gating: only agents that declare
# this pack in their manifest receive the DISPATCH_* vars.
DISPATCH_PACK_NAME = "dispatch"


@dataclass
class PackDispatchExtras:
    """Everything the dispatcher needs to inject for a session's packs."""

    prompt_files: list[str]
    """Paths inside the container, suitable for --append-system-prompt-file."""

    mcp_config_path: str | None
    """Host path to a temporary ``.mcp.json``, or None if no pack contributes one."""

    env: dict[str, str]
    """Env vars to inject into the docker exec call."""


def _load_packs_for_agent(
    agent_name: str,
    manifest_path: Path | None = None,
) -> list[str]:
    """Read ``packs:`` from the agent's manifest. Empty list if absent."""
    path = manifest_path or Path(str(DEFAULT_MANIFEST_PATH_TEMPLATE).format(agent=agent_name))
    if not path.exists():
        return []
    with open(path) as f:
        manifest = yaml.safe_load(f) or {}
    if not isinstance(manifest, dict):
        return []
    raw = manifest.get("packs") or []
    if not isinstance(raw, list):
        logger.warning("agent %s: 'packs' is not a list — ignoring", agent_name)
        return []
    return [str(name) for name in raw]


def _merged_mcp_config(packs: list[Pack]) -> dict[str, Any] | None:
    """Merge each pack's ``mcp.json`` into a single config dict.

    Returns None if no pack ships an ``mcp.json`` — caller should skip the
    ``--mcp-config`` flag entirely in that case.
    """
    merged_servers: dict[str, Any] = {}
    seen = False
    for pack in packs:
        if pack.mcp_path is None:
            continue
        seen = True
        with open(pack.mcp_path) as f:
            raw = json.load(f)
        servers = raw.get("mcpServers") or raw.get("servers") or {}
        for name, cfg in servers.items():
            if name in merged_servers:
                logger.warning(
                    "pack %s: MCP server name %r already declared by another pack; later pack wins",
                    pack.name,
                    name,
                )
            merged_servers[name] = cfg
    if not seen:
        return None
    return {"mcpServers": merged_servers}


def _env_from_secrets(packs: list[Pack], store: SecretStore) -> dict[str, Any]:
    """Resolve ``needs:`` entries against the secret store.

    A pack's ``needs: [GITHUB_TOKEN]`` is interpreted as: look up the
    pack's secret block, and inject every key in ``needs`` that exists
    there (case-insensitive match against the block's keys). The pack's
    ``authenticate.py`` is responsible for storing values under those keys.

    Missing secrets are logged and skipped — the dispatcher will still run,
    and the agent will see a missing-token error from its tool, which is
    surfacing the same problem one layer later.
    """
    env: dict[str, str] = {}
    for pack in packs:
        if not pack.needs:
            continue
        block = store.get(pack.name)
        if not block:
            logger.warning(
                "pack %s declares needs %s but no secret block stored — skipping",
                pack.name,
                pack.needs,
            )
            continue
        # Case-insensitive lookup so packs can store either GITHUB_TOKEN or token.
        lower_block = {k.lower(): v for k, v in block.items()}
        for need in pack.needs:
            value = block.get(need) or lower_block.get(need.lower())
            if value is None:
                logger.warning("pack %s: secret %r not found in store", pack.name, need)
                continue
            env[need] = str(value)
    return env


def pack_cli_extras(
    agent_name: str,
    *,
    manifest_path: Path | None = None,
    packs_dir: Path | None = None,
    secret_store: SecretStore | None = None,
    tmp_dir: Path | None = None,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> PackDispatchExtras:
    """Compute pack-derived dispatch extras for ``agent_name``.

    Returns empty extras if the agent has no ``packs:`` key, no listed pack
    exists, or all listed packs are empty. This is the dormant-PR-2 guarantee.

    When the agent declares the ``dispatch`` pack and ``channel`` /
    ``thread_ts`` are supplied, inject ``DISPATCH_CHANNEL`` /
    ``DISPATCH_THREAD_TS`` / ``DISPATCH_AGENT`` env vars so the dispatch
    handler can spawn follow-up dispatches from inside the agent without
    explicit ``--channel`` / ``--thread-ts`` / ``--agent`` flags. Agents
    without the dispatch pack never see these vars — same gating as
    ``GITHUB_TOKEN`` for the github pack.
    """
    declared = _load_packs_for_agent(agent_name, manifest_path)
    if not declared:
        return PackDispatchExtras(prompt_files=[], mcp_config_path=None, env={})

    available = discover_packs(packs_dir)
    resolved: list[Pack] = []
    for name in declared:
        pack = available.get(name)
        if pack is None:
            logger.warning("agent %s declares unknown pack %r — skipping", agent_name, name)
            continue
        resolved.append(pack)

    if not resolved:
        return PackDispatchExtras(prompt_files=[], mcp_config_path=None, env={})

    prompt_files = [f"{CONTAINER_PACKS_DIR}/{pack.name}/prompt.md" for pack in resolved if pack.prompt_path is not None]

    merged = _merged_mcp_config(resolved)
    mcp_config_path: str | None = None
    if merged is not None:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".mcp.json",
            delete=False,
            dir=str(tmp_dir) if tmp_dir is not None else None,
        )
        json.dump(merged, tmp)
        tmp.close()
        mcp_config_path = tmp.name

    store = secret_store or SecretStore()
    env = _env_from_secrets(resolved, store)

    if any(pack.name == DISPATCH_PACK_NAME for pack in resolved):
        if channel:
            env["DISPATCH_CHANNEL"] = channel
        if thread_ts:
            env["DISPATCH_THREAD_TS"] = thread_ts
        env["DISPATCH_AGENT"] = agent_name

    return PackDispatchExtras(
        prompt_files=prompt_files,
        mcp_config_path=mcp_config_path,
        env=env,
    )
