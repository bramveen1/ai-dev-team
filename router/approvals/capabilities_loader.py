"""Capability configuration loader for the approval flow.

Loads agent capability definitions from ``config/capabilities.yaml``
and provides lookup functions for resolving capability instances at
runtime (e.g. to determine permissions for button resolution).

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

CAPABILITIES_PATH = Path(__file__).parent.parent.parent / "config" / "capabilities.yaml"


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


def _load(path: Path | None = None) -> dict[str, AgentCapabilities]:
    """Parse capabilities.yaml into AgentCapabilities dataclasses."""
    config_path = path or CAPABILITIES_PATH
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    agents_raw: dict[str, Any] = raw.get("agents", {})
    result: dict[str, AgentCapabilities] = {}

    for agent_name, agent_data in agents_raw.items():
        caps: dict[str, list[CapabilityInstance]] = {}
        for cap_type, instances in agent_data.get("capabilities", {}).items():
            caps[cap_type] = [_parse_instance(inst) for inst in instances]
        result[agent_name] = AgentCapabilities(agent=agent_data.get("agent", agent_name), capabilities=caps)

    return result


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
        path: Optional path to capabilities.yaml (for testing).

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
