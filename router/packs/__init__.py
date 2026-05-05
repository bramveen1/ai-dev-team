"""Pack runtime — discovery, loading, and runtime hooks for capability packs.

A *pack* is a self-contained directory under ``packs/<name>/`` that bundles
everything an agent needs for one capability:

- ``pack.yaml`` — manifest (name, description, secrets needed, approval rules)
- ``prompt.md`` — agent-facing description appended to the system prompt
- ``mcp.json`` — optional MCP server config merged into the agent's session
- ``authenticate.py`` — optional async helper that walks a user through token
  acquisition (e.g. device-code flow, paste a PAT)

This package is intentionally minimal — pack discovery and rendering should
not require the heavyweight ``capabilities/`` framework. See
``docs/capabilities-simplification.md`` for the migration that introduced it.
"""

from router.packs.grants import (
    GrantCommand,
    ListPacksCommand,
    RevokeCommand,
    WhoHasCommand,
    handle_grant,
    handle_list_packs,
    handle_revoke,
    handle_who_has,
    maybe_handle_pack_command,
    parse_command,
)
from router.packs.loader import (
    Pack,
    PackError,
    discover_packs,
    load_pack,
)
from router.packs.secret_store import (
    SECRETS_PATH,
    SecretStore,
)

__all__ = [
    "GrantCommand",
    "ListPacksCommand",
    "Pack",
    "PackError",
    "RevokeCommand",
    "SECRETS_PATH",
    "SecretStore",
    "WhoHasCommand",
    "discover_packs",
    "handle_grant",
    "handle_list_packs",
    "handle_revoke",
    "handle_who_has",
    "load_pack",
    "maybe_handle_pack_command",
    "parse_command",
]
