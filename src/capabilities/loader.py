"""Capability config loader — parses and validates capabilities.yaml and providers.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from src.capabilities.models import (
    PERMISSION_VOCABULARY,
    AgentCapabilities,
    CapabilityInstance,
    ProvidersConfig,
)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


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


def load_config(path: str | Path | None = None) -> dict[str, AgentCapabilities]:
    """Load and validate capabilities.yaml, returning a dict of agent_name -> AgentCapabilities.

    Validates:
    - YAML structure matches Pydantic models
    - All providers referenced exist in the provider registry
    - All permissions are valid for their capability type
    - No duplicate instance names within a capability type for an agent
    """
    if path is None:
        path = DEFAULT_CONFIG_DIR / "capabilities.yaml"
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Capabilities config not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "agents" not in raw:
        raise ConfigError(f"Capabilities config must contain an 'agents' key: {path}")

    # Load providers for cross-validation
    providers_path = path.parent / "providers.yaml"
    providers = load_providers(providers_path)

    agents: dict[str, AgentCapabilities] = {}

    for agent_name, agent_data in raw["agents"].items():
        if "agent" not in agent_data:
            agent_data["agent"] = agent_name

        try:
            agent_caps = AgentCapabilities(**agent_data)
        except ValidationError as e:
            raise ConfigError(f"Invalid config for agent '{agent_name}': {e}") from e

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
