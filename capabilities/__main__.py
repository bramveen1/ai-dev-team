"""CLI entry points for the capability framework.

Usage:
    python -m capabilities render <agent_name>     — render system prompt summary
    python -m capabilities mcp_config <agent_name> — output .mcp.json for agent
    python -m capabilities refresh_tokens          — refresh expired OAuth tokens
"""

from __future__ import annotations

import asyncio
import json
import sys

from capabilities.loader import ConfigError, load_providers
from capabilities.mcp_namespacer import generate_mcp_config
from capabilities.prompt_renderer import render_capability_summary
from capabilities.secrets import SecretStore


async def _refresh_tokens() -> list[str]:
    """Refresh expired OAuth tokens for all providers that need it.

    Returns list of provider names that were refreshed.
    """
    from capabilities.oauth import ensure_valid_token

    store = SecretStore()
    providers = load_providers()
    refreshed: list[str] = []

    for provider_name, provider_config in providers.providers.items():
        if not provider_config.oauth:
            continue

        # Determine the secrets provider name from secrets_map
        secrets_provider = None
        for mapping in provider_config.secrets_map.values():
            parts = mapping.split(":", 1)
            if len(parts) == 2:
                secrets_provider = parts[0]
                break

        if not secrets_provider:
            continue

        if not store.needs_refresh(secrets_provider):
            continue

        try:
            await ensure_valid_token(store, secrets_provider, provider_config.oauth)
            refreshed.append(secrets_provider)
        except Exception as e:
            print(f"Warning: failed to refresh {secrets_provider}: {e}", file=sys.stderr)

    return refreshed


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m capabilities <command> [args]", file=sys.stderr)
        print("Commands: render <agent>, mcp_config <agent>, refresh_tokens", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "render":
            if len(sys.argv) < 3:
                print("Usage: python -m capabilities render <agent_name>", file=sys.stderr)
                sys.exit(1)
            print(render_capability_summary(sys.argv[2]))

        elif command == "mcp_config":
            if len(sys.argv) < 3:
                print("Usage: python -m capabilities mcp_config <agent_name>", file=sys.stderr)
                sys.exit(1)
            agent_name = sys.argv[2]
            # Try to use secrets store if available
            store = None
            try:
                store = SecretStore()
                if not store.secrets_dir.exists():
                    store = None
            except Exception:
                store = None

            config = generate_mcp_config(agent_name, secret_store=store)
            print(json.dumps(config, indent=2))

        elif command == "refresh_tokens":
            refreshed = asyncio.run(_refresh_tokens())
            if refreshed:
                for p in refreshed:
                    print(f"Refreshed: {p}")
            else:
                print("No tokens needed refreshing.")

        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            print("Commands: render <agent>, mcp_config <agent>, refresh_tokens", file=sys.stderr)
            sys.exit(1)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
