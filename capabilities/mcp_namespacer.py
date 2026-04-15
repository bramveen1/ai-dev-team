"""MCP namespacer — generates .mcp.json from an agent's capability config."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from capabilities.loader import ConfigError, get_agent_capabilities, load_providers
from capabilities.models import CapabilityInstance, ProviderConfig

if TYPE_CHECKING:
    from capabilities.secrets import SecretStore

logger = logging.getLogger(__name__)

_ENV_REF_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


def generate_mcp_config(
    agent_name: str,
    config_path: str | Path | None = None,
    providers_path: str | Path | None = None,
    secret_store: SecretStore | None = None,
) -> dict:
    """Generate the .mcp.json contents for an agent's container.

    Each capability instance becomes one MCP server entry named
    ``{capability_type}_{instance_name}``.

    Args:
        agent_name: Agent to generate config for.
        config_path: Path to capabilities.yaml.
        providers_path: Path to providers.yaml.
        secret_store: Optional SecretStore for resolving ``${VAR}`` references
            from the secrets store instead of leaving them for runtime expansion.

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
            server_entry = _build_server_entry(cap_type, inst, provider, secret_store)
            mcp_servers[namespace] = server_entry

    return {"mcpServers": mcp_servers}


def _build_server_entry(
    cap_type: str,
    inst: CapabilityInstance,
    provider: ProviderConfig,
    secret_store: SecretStore | None = None,
) -> dict:
    """Build a single MCP server entry for one capability instance."""
    env = _resolve_env(cap_type, inst, provider, secret_store)

    return {
        "command": provider.command,
        "args": list(provider.args),
        "env": env,
    }


def _resolve_env(
    cap_type: str,
    inst: CapabilityInstance,
    provider: ProviderConfig,
    secret_store: SecretStore | None = None,
) -> dict[str, str]:
    """Resolve environment variables from the provider's env_template.

    Substitutions:
    - ``{account}`` -> instance account value
    - ``{computed_scopes}`` -> comma-separated unique scopes for granted permissions
    - ``${VAR_NAME}`` -> resolved from secrets store (if provided), then os.environ,
      then kept as-is for runtime expansion
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

        # Resolve ${VAR_NAME} from secrets store
        if secret_store is not None:
            match = _ENV_REF_PATTERN.match(value)
            if match:
                var_name = match.group(1)
                resolved = secret_store.resolve_env_value(var_name, provider.secrets_map)
                if resolved is not None:
                    value = resolved
                else:
                    logger.warning("Could not resolve ${%s} from secrets store or environment", var_name)

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
