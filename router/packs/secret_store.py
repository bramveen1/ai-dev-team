"""Secret store — single-file JSON store keyed by pack name.

Replaces the per-provider files under ``config/secrets/<name>.json`` from
the old framework. Everything lives under one path (``data/secrets.json``)
that is gitignored and managed by the router.

Shape:

```json
{
  "github": {"token": "...", "obtained_at": 1730000000},
  "zoho-mail": {"refresh_token": "...", "access_token": "...", "expires_at": ...}
}
```

Pack authors decide what shape their secrets take — the store treats values
as opaque JSON. ``needs:`` in ``pack.yaml`` declares the **env var** names
to inject at dispatch time; the store's keys are pack names; the mapping
between pack-secret-fields and env-var names is the pack's responsibility
(see ``Pack.env_for(secret)`` once implemented in PR 3).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from router.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SECRETS_PATH = REPO_ROOT / "data" / "secrets.json"


class SecretStore:
    """Read/write a single JSON file keyed by pack name.

    Operations are point-in-time — the store re-reads the file on each call,
    so concurrent writers can't lose data through stale in-memory copies.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else SECRETS_PATH

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{self.path} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{self.path} root must be a JSON object")
        return data

    def _write_all(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data, indent=2, sort_keys=True)

    def get(self, pack: str) -> dict[str, Any]:
        """Return the secret block for ``pack`` (empty dict if unset)."""
        return dict(self._read_all().get(pack, {}))

    def set(self, pack: str, value: dict[str, Any]) -> None:
        """Replace the secret block for ``pack``."""
        data = self._read_all()
        data[pack] = dict(value)
        self._write_all(data)

    def delete(self, pack: str) -> bool:
        """Remove the secret block for ``pack``. Returns True if it existed."""
        data = self._read_all()
        if pack in data:
            del data[pack]
            self._write_all(data)
            return True
        return False

    def has(self, pack: str) -> bool:
        return pack in self._read_all()

    def get_str(self, key: str) -> str | None:
        """Return a top-level scalar string value (None if absent or not a str)."""
        val = self._read_all().get(key)
        return val if isinstance(val, str) else None

    def set_str(self, key: str, value: str) -> None:
        """Set a top-level scalar string value. Empty string removes the key."""
        data = self._read_all()
        if value == "":
            data.pop(key, None)
        else:
            data[key] = value
        self._write_all(data)
