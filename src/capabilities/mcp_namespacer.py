"""MCP namespacer — generates .mcp.json from an agent's capability config."""

from __future__ import annotations

from pathlib import Path

from src.capabilities.loader import ConfigError, get_agent_capabilities, load_providers
from src.capabilities.models import CapabilityInstance, ProviderConfig


def generate_mcp_config(
    agent_name: str,
    config_path: str | Path | None = None,
    providers_path: str | Path | None = None,
) -> dict:
    """Generate the .mcp.json contents for an agent's container.

    Each capability instance becomes one MCP server entry named
    ``{capability_type}_{instance_name}``.

    Returns a dict with a ``mcpServers`` key mapping namespace -> server config.
    """
    agent_caps = get_agent_capabilities(agent_name, config_path)
    providers = load_providers(providers_path)

    mcp_servers: dict[str, dict] = {}
    seen_namespaces: set[str] = set()

    for cap_type, instances in agent_caps.capabilities.items():
        for inst in instances:
            namespace = f"{cap_type}_{inst.instance}"

            if namespace in seen_namespaces:
                raise ConfigError(
                    f"Namespace collision: '{namespace}' appears more than once "
                    f"for agent '{agent_name}'. Instance names must be unique within a capability type."
                )
            seen_namespaces.add(namespace)

            provider = providers.providers[inst.provider]
            server_entry = _build_server_entry(cap_type, inst, provider)
            mcp_servers[namespace] = server_entry

    return {"mcpServers": mcp_servers}


def _build_server_entry(
    cap_type: str,
    inst: CapabilityInstance,
    provider: ProviderConfig,
) -> dict:
    """Build a single MCP server entry for one capability instance."""
    env = _resolve_env(cap_type, inst, provider)

    return {
        "command": provider.command,
        "args": list(provider.args),
        "env": env,
    }


def _resolve_env(
    cap_type: str,
    inst: CapabilityInstance,
    provider: ProviderConfig,
) -> dict[str, str]:
    """Resolve environment variables from the provider's env_template.

    Substitutions:
    - ``{account}`` -> instance account value
    - ``{computed_scopes}`` -> comma-separated unique scopes for granted permissions
    - ``${VAR_NAME}`` -> kept as-is (resolved at container runtime)
    """
    env: dict[str, str] = {}

    for key, template in provider.env_template.items():
        value = template

        # Substitute {account}
        value = value.replace("{account}", inst.account)

        # Substitute {computed_scopes}
        if "{computed_scopes}" in value:
            scopes = _compute_scopes(cap_type, inst, provider)
            value = value.replace("{computed_scopes}", scopes)

        env[key] = value

    return env


def _compute_scopes(
    cap_type: str,
    inst: CapabilityInstance,
    provider: ProviderConfig,
) -> str:
    """Compute the comma-separated unique scopes for an instance's granted permissions."""
    scope_map = provider.permission_scopes.get(cap_type, {})
    scopes: list[str] = []
    seen: set[str] = set()

    for perm in inst.permissions:
        scope = scope_map.get(perm)
        if scope and scope not in seen:
            scopes.append(scope)
            seen.add(scope)

    return ",".join(scopes)
