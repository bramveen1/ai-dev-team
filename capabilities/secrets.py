"""Generic file-based secrets store for provider credentials.

Each provider gets a JSON file at ``config/secrets/{provider}.json``.
The store provides read/write access to individual keys and supports
checking token expiration for OAuth providers.

Storage layout::

    config/secrets/
      m365.json      # {client_id, tenant_id, client_secret, access_token, refresh_token, expires_at, ...}
      zoho.json      # {api_key, ...}
      github.json    # {token, ...}

Security: The ``config/secrets/`` directory is gitignored. Files are
plain JSON — appropriate for a self-hosted Docker deployment. For
production use, consider integrating with a vault or secrets manager.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SECRETS_DIR = Path(__file__).resolve().parent.parent / "config" / "secrets"

# Refresh tokens 5 minutes before they expire to avoid race conditions.
REFRESH_BUFFER_SECONDS = 300


class SecretsError(Exception):
    """Raised when a secret cannot be loaded or is missing."""


class SecretStore:
    """File-based secrets store for provider credentials.

    Each provider maps to a JSON file at ``{secrets_dir}/{provider}.json``.

    Args:
        secrets_dir: Directory containing provider secret files.
            Defaults to ``config/secrets/`` relative to the project root.
    """

    def __init__(self, secrets_dir: str | Path | None = None) -> None:
        self._dir = Path(secrets_dir) if secrets_dir else DEFAULT_SECRETS_DIR
        self._cache: dict[str, dict[str, Any]] = {}

    @property
    def secrets_dir(self) -> Path:
        """Return the secrets directory path."""
        return self._dir

    def _file_path(self, provider: str) -> Path:
        """Return the path to a provider's secrets file."""
        return self._dir / f"{provider}.json"

    def load(self, provider: str) -> dict[str, Any]:
        """Load all secrets for a provider from disk.

        Returns the full dict from the provider's JSON file.
        Results are cached in memory; call ``invalidate()`` to clear.

        Raises:
            SecretsError: If the file doesn't exist or is invalid JSON.
        """
        if provider in self._cache:
            return self._cache[provider]

        path = self._file_path(provider)
        if not path.exists():
            raise SecretsError(f"Secrets file not found: {path}")

        try:
            with open(path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise SecretsError(f"Invalid JSON in {path}: {e}") from e

        if not isinstance(data, dict):
            raise SecretsError(f"Secrets file must contain a JSON object: {path}")

        self._cache[provider] = data
        return data

    def save(self, provider: str, data: dict[str, Any]) -> None:
        """Write the full secrets dict for a provider to disk.

        Creates the secrets directory if it doesn't exist.
        Invalidates the cache for this provider.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._file_path(provider)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        self._cache.pop(provider, None)
        logger.info("Saved secrets for provider %s to %s", provider, path)

    def get(self, provider: str, key: str) -> str | None:
        """Get a single secret value for a provider.

        Args:
            provider: Provider name (e.g. "m365", "zoho", "github").
            key: Secret key (e.g. "access_token", "client_id").

        Returns:
            The secret value as a string, or None if the provider file
            doesn't exist or the key is missing.
        """
        try:
            data = self.load(provider)
        except SecretsError:
            return None
        value = data.get(key)
        if value is None:
            return None
        return str(value)

    def set(self, provider: str, key: str, value: Any) -> None:
        """Set a single secret value and persist to disk.

        Loads existing secrets (or starts with empty dict), updates the key,
        and writes back to disk.
        """
        try:
            data = self.load(provider)
        except SecretsError:
            data = {}
        data[key] = value
        self.save(provider, data)

    def needs_refresh(self, provider: str) -> bool:
        """Check if a provider's access token is expired or about to expire.

        Returns True if:
        - The provider has an ``expires_at`` field (unix timestamp)
        - Current time + REFRESH_BUFFER_SECONDS >= expires_at

        Returns False if no ``expires_at`` field exists (e.g. API key providers).
        """
        try:
            data = self.load(provider)
        except SecretsError:
            return False

        expires_at = data.get("expires_at")
        if expires_at is None:
            return False

        try:
            expires_ts = float(expires_at)
        except (TypeError, ValueError):
            logger.warning("Invalid expires_at value for provider %s: %s", provider, expires_at)
            return True  # If we can't parse it, assume expired

        return time.time() + REFRESH_BUFFER_SECONDS >= expires_ts

    def invalidate(self, provider: str | None = None) -> None:
        """Clear cached secrets.

        Args:
            provider: Clear cache for this provider only. If None, clear all.
        """
        if provider:
            self._cache.pop(provider, None)
        else:
            self._cache.clear()

    def resolve_env_value(self, var_name: str, secrets_map: dict[str, str]) -> str | None:
        """Resolve an environment variable name to its secret value.

        Uses the provider's ``secrets_map`` to look up the provider and key,
        then returns the value from the secrets store. Falls back to
        ``os.environ`` if the secrets store doesn't have the value.

        Args:
            var_name: Environment variable name (e.g. "M365_ACCESS_TOKEN").
            secrets_map: Mapping from env var names to "provider:key" strings.

        Returns:
            The resolved value, or None if not found anywhere.
        """
        # Try secrets_map first
        mapping = secrets_map.get(var_name)
        if mapping:
            parts = mapping.split(":", 1)
            if len(parts) == 2:
                provider, key = parts
                value = self.get(provider, key)
                if value is not None:
                    return value

        # Fall back to os.environ
        env_value = os.environ.get(var_name)
        if env_value:
            return env_value

        return None
