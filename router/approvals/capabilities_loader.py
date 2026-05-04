"""Capability configuration loader for the approval flow.

In production, agent capabilities are discovered from
``config/agents/*/agent.yaml`` (one manifest per agent). For backward
compatibility (and test fixtures), :func:`load_capabilities` also accepts
a single legacy aggregate file with the top-level ``agents:`` schema.

Uses lightweight dataclasses rather than the ``capabilities.models``
Pydantic models so the router container doesn't need the
``capabilities`` package on its Python path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_AGENTS_DIR = Path(__file__).parent.parent.parent / "config" / "agents"


@dataclass
class CapabilityInstance:
    """Minimal capability instance — just the fields the approval flow needs."""

    instance: str
    provider: str
    account: str
    ownership: str
    permissions: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentCapabilities:
    """All capabilities for a single agent, grouped by type."""

    agent: str
    capabilities: dict[str, list[CapabilityInstance]] = field(default_factory=dict)


# Module-level cache — loaded once on first access.
_cache: dict[str, AgentCapabilities] | None = None


def _parse_instance(data: dict[str, Any]) -> CapabilityInstance:
    """Parse a single capability instance from raw YAML data."""
    return CapabilityInstance(
        instance=data["instance"],
        provider=data["provider"],
        account=data.get("account", ""),
        ownership=data.get("ownership", "self"),
        permissions=data.get("permissions", []),
        config=data.get("config", {}),
    )


def _parse_capabilities_block(
    capabilities_block: dict[str, Any] | None,
) -> dict[str, list[CapabilityInstance]]:
    """Parse a ``capabilities`` mapping into per-type lists of instances."""
    caps: dict[str, list[CapabilityInstance]] = {}
    for cap_type, instances in (capabilities_block or {}).items():
        caps[cap_type] = [_parse_instance(inst) for inst in instances]
    return caps


def _load_per_agent(agents_dir: Path) -> dict[str, AgentCapabilities]:
    """Discover and parse ``<agents_dir>/<name>/agent.yaml`` for every agent."""
    result: dict[str, AgentCapabilities] = {}
    if not agents_dir.exists():
        return result

    for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
        manifest_path = agent_dir / "agent.yaml"
        if not manifest_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = yaml.safe_load(f) or {}

        if not isinstance(manifest, dict):
            logger.warning("Skipping %s — not a YAML mapping", manifest_path)
            continue

        agent_name = agent_dir.name
        result[agent_name] = AgentCapabilities(
            agent=agent_name,
            capabilities=_parse_capabilities_block(manifest.get("capabilities")),
        )

    return result


def _load_legacy_aggregate(path: Path) -> dict[str, AgentCapabilities]:
    """Parse a legacy single-file aggregate (top-level ``agents:`` schema)."""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    agents_raw: dict[str, Any] = raw.get("agents", {})
    result: dict[str, AgentCapabilities] = {}

    for agent_name, agent_data in agents_raw.items():
        result[agent_name] = AgentCapabilities(
            agent=agent_data.get("agent", agent_name),
            capabilities=_parse_capabilities_block(agent_data.get("capabilities")),
        )

    return result


def _load(path: Path | None = None) -> dict[str, AgentCapabilities]:
    """Load agent capabilities.

    - ``None`` → discover from ``config/agents/*/agent.yaml`` (production).
    - directory → discover from ``<dir>/*/agent.yaml``.
    - file → parse as a legacy aggregate file with top-level ``agents:`` schema.
    """
    if path is None:
        return _load_per_agent(DEFAULT_AGENTS_DIR)
    if path.is_dir():
        return _load_per_agent(path)
    return _load_legacy_aggregate(path)


def load_capabilities(path: Path | None = None) -> dict[str, AgentCapabilities]:
    """Load and cache agent capabilities from the YAML config.

    Returns a dict mapping agent name to AgentCapabilities.
    """
    global _cache
    if _cache is None:
        _cache = _load(path)
        logger.debug("Loaded capabilities for %d agents", len(_cache))
    return _cache


def reset_cache() -> None:
    """Clear the capabilities cache (useful for testing)."""
    global _cache
    _cache = None


def get_capability_instance(
    agent_name: str,
    capability_type: str,
    instance_name: str,
    path: Path | None = None,
) -> CapabilityInstance | None:
    """Look up a specific capability instance for an agent.

    Args:
        agent_name: The agent name (e.g. "lisa").
        capability_type: The capability type (e.g. "email").
        instance_name: The instance name (e.g. "mine", "bram").
        path: Optional override (directory of agent manifests, or a legacy
            aggregate file). Defaults to ``config/agents/``.

    Returns:
        The matching CapabilityInstance, or None if not found.
    """
    caps = load_capabilities(path)
    agent_caps = caps.get(agent_name)
    if agent_caps is None:
        return None

    instances = agent_caps.capabilities.get(capability_type, [])
    for inst in instances:
        if inst.instance == instance_name:
            return inst

    return None
