"""DEPRECATED — scheduled for removal. See docs/capabilities-simplification.md (issues #81–#88).

Capability config loader — parses and validates per-agent manifests, baseline.yaml, and providers.yaml.

In production, capabilities are discovered from ``config/agents/*/agent.yaml``
(one manifest per agent). For backward compatibility (and test fixtures),
:func:`load_config` also accepts a single legacy aggregate file with the
top-level ``agents:`` schema.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from pydantic import ValidationError

from capabilities.models import (
    PERMISSION_VOCABULARY,
    AgentCapabilities,
    CapabilityInstance,
    ProvidersConfig,
)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_AGENTS_DIR = DEFAULT_CONFIG_DIR / "agents"


class ConfigError(Exception):
    """Raised when capability configuration is invalid."""


def load_providers(path: str | Path | None = None) -> ProvidersConfig:
    """Load and validate the provider registry from providers.yaml."""
    if path is None:
        path = DEFAULT_CONFIG_DIR / "providers.yaml"
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Provider config not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "providers" not in raw:
        raise ConfigError(f"Provider config must contain a 'providers' key: {path}")

    try:
        return ProvidersConfig(**raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid provider config: {e}") from e


def load_baseline(path: str | Path | None = None) -> dict[str, list[dict]]:
    """Load baseline capabilities from baseline.yaml.

    Returns a dict of capability_type -> list of raw instance dicts.
    Returns an empty dict if baseline.yaml does not exist.
    """
    if path is None:
        path = DEFAULT_CONFIG_DIR / "baseline.yaml"
    path = Path(path)

    if not path.exists():
        return {}

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "capabilities" not in raw:
        return {}

    return raw["capabilities"]


def _resolve_baseline_for_agent(
    agent_name: str,
    baseline: dict[str, list[dict]],
) -> dict[str, list[CapabilityInstance]]:
    """Resolve baseline capabilities for a specific agent.

    Substitutes ``{agent}`` placeholders in account fields with the agent name.
    """
    resolved: dict[str, list[CapabilityInstance]] = {}

    for cap_type, instances in baseline.items():
        resolved_instances: list[CapabilityInstance] = []
        for inst_data in instances:
            inst_copy = copy.deepcopy(inst_data)
            # Substitute {agent} placeholder in account field
            if isinstance(inst_copy.get("account"), str):
                inst_copy["account"] = inst_copy["account"].replace("{agent}", agent_name)
            try:
                resolved_instances.append(CapabilityInstance(**inst_copy))
            except ValidationError as e:
                raise ConfigError(f"Invalid baseline config for {cap_type}: {e}") from e
        resolved[cap_type] = resolved_instances

    return resolved


def _merge_baseline(
    agent_caps: AgentCapabilities,
    baseline: dict[str, list[dict]],
) -> AgentCapabilities:
    """Merge baseline capabilities into an agent's capability set.

    Rules:
    - Baseline capabilities are added for capability types the agent doesn't already have.
    - For capability types the agent does have, baseline instances are added only if no
      instance with the same name already exists (agent-specific config wins).
    """
    agent_name = agent_caps.agent
    resolved_baseline = _resolve_baseline_for_agent(agent_name, baseline)

    merged_caps = dict(agent_caps.capabilities)

    for cap_type, baseline_instances in resolved_baseline.items():
        if cap_type not in merged_caps:
            # Agent has no config for this capability — use all baseline instances
            merged_caps[cap_type] = list(baseline_instances)
        else:
            # Agent already has this capability — add baseline instances that don't conflict
            existing_names = {inst.instance for inst in merged_caps[cap_type]}
            for b_inst in baseline_instances:
                if b_inst.instance not in existing_names:
                    merged_caps[cap_type] = list(merged_caps[cap_type]) + [b_inst]

    return AgentCapabilities(agent=agent_name, capabilities=merged_caps)


def load_config(path: str | Path | None = None) -> dict[str, AgentCapabilities]:
    """Load and validate agent capabilities.

    Three modes (chosen by ``path``):

    - ``None`` → discover from ``config/agents/*/agent.yaml`` (production).
    - directory → discover from ``<dir>/*/agent.yaml`` (test fixtures, alt configs).
    - file → parse as a legacy aggregate file with top-level ``agents:`` schema.

    Automatically merges baseline capabilities from ``baseline.yaml`` (if present).

    Validates:
    - YAML structure matches Pydantic models
    - All providers referenced exist in the provider registry
    - All permissions are valid for their capability type
    - No duplicate instance names within a capability type for an agent
    """
    if path is None:
        agents_dir = DEFAULT_AGENTS_DIR
        config_dir = DEFAULT_CONFIG_DIR
        return _load_per_agent(agents_dir, config_dir)

    path = Path(path)

    if path.is_dir():
        return _load_per_agent(path, path.parent)

    if not path.exists():
        raise ConfigError(f"Capabilities config not found: {path}")

    return _load_legacy_aggregate(path)


def _load_per_agent(
    agents_dir: Path,
    config_dir: Path,
) -> dict[str, AgentCapabilities]:
    """Discover and parse ``<agents_dir>/<name>/agent.yaml`` for every agent."""
    if not agents_dir.exists():
        raise ConfigError(f"Agents directory not found: {agents_dir}")

    providers = load_providers(config_dir / "providers.yaml")
    baseline = load_baseline(config_dir / "baseline.yaml")

    agents: dict[str, AgentCapabilities] = {}

    for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
        manifest_path = agent_dir / "agent.yaml"
        if not manifest_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = yaml.safe_load(f) or {}

        if not isinstance(manifest, dict):
            raise ConfigError(f"Invalid agent manifest (not a YAML mapping): {manifest_path}")

        agent_name = agent_dir.name
        capabilities_block = manifest.get("capabilities") or {}

        try:
            agent_caps = AgentCapabilities(agent=agent_name, capabilities=capabilities_block)
        except ValidationError as e:
            raise ConfigError(f"Invalid capabilities for agent '{agent_name}' ({manifest_path}): {e}") from e

        if baseline:
            agent_caps = _merge_baseline(agent_caps, baseline)

        _validate_agent_capabilities(agent_name, agent_caps, providers)
        agents[agent_name] = agent_caps

    return agents


def _load_legacy_aggregate(path: Path) -> dict[str, AgentCapabilities]:
    """Parse a legacy single-file aggregate (top-level ``agents:`` schema)."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "agents" not in raw:
        raise ConfigError(f"Capabilities config must contain an 'agents' key: {path}")

    providers = load_providers(path.parent / "providers.yaml")
    baseline = load_baseline(path.parent / "baseline.yaml")

    agents: dict[str, AgentCapabilities] = {}

    for agent_name, agent_data in raw["agents"].items():
        if "agent" not in agent_data:
            agent_data["agent"] = agent_name

        try:
            agent_caps = AgentCapabilities(**agent_data)
        except ValidationError as e:
            raise ConfigError(f"Invalid config for agent '{agent_name}': {e}") from e

        if baseline:
            agent_caps = _merge_baseline(agent_caps, baseline)

        _validate_agent_capabilities(agent_name, agent_caps, providers)
        agents[agent_name] = agent_caps

    return agents


def get_agent_capabilities(
    agent_name: str,
    config_path: str | Path | None = None,
) -> AgentCapabilities:
    """Load config and return capabilities for a specific agent.

    Raises ConfigError if the agent is not found.
    """
    agents = load_config(config_path)
    if agent_name not in agents:
        raise ConfigError(f"Agent '{agent_name}' not found in capabilities config. Available: {sorted(agents.keys())}")
    return agents[agent_name]


def _validate_agent_capabilities(
    agent_name: str,
    agent_caps: AgentCapabilities,
    providers: ProvidersConfig,
) -> None:
    """Cross-validate an agent's capabilities against the provider registry and permission vocabulary."""
    for cap_type, instances in agent_caps.capabilities.items():
        _validate_instances(agent_name, cap_type, instances, providers)


def _validate_instances(
    agent_name: str,
    cap_type: str,
    instances: list[CapabilityInstance],
    providers: ProvidersConfig,
) -> None:
    """Validate all instances for a given capability type."""
    seen_names: set[str] = set()

    for inst in instances:
        # Check for duplicate instance names within a capability type
        if inst.instance in seen_names:
            raise ConfigError(
                f"Agent '{agent_name}': duplicate instance name '{inst.instance}' in capability '{cap_type}'"
            )
        seen_names.add(inst.instance)

        # Validate provider exists
        if inst.provider not in providers.providers:
            raise ConfigError(
                f"Agent '{agent_name}', {cap_type}/{inst.instance}: "
                f"unknown provider '{inst.provider}'. "
                f"Available: {sorted(providers.providers.keys())}"
            )

        provider = providers.providers[inst.provider]

        # Validate provider supports this capability type
        if cap_type not in provider.capabilities:
            raise ConfigError(
                f"Agent '{agent_name}', {cap_type}/{inst.instance}: "
                f"provider '{inst.provider}' does not support capability '{cap_type}'. "
                f"Supported: {provider.capabilities}"
            )

        # Validate permissions against vocabulary
        if cap_type in PERMISSION_VOCABULARY:
            valid_perms = PERMISSION_VOCABULARY[cap_type]
            for perm in inst.permissions:
                if perm not in valid_perms:
                    raise ConfigError(
                        f"Agent '{agent_name}', {cap_type}/{inst.instance}: "
                        f"invalid permission '{perm}'. "
                        f"Valid for '{cap_type}': {sorted(valid_perms)}"
                    )
