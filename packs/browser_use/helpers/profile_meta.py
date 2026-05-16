"""Per-profile metadata — ``meta.json`` inside each profile dir.

Two pieces of state live here:

- ``login_enabled``: opt-in flag for the ``login`` verb. Profiles
  without ``login_enabled: true`` will have ``login`` /
  ``session_status`` / auto-retry refused. The rollout plan in issue
  #147 puts this behind an opt-in until a clean week of pathtohired
  use proves the verb out.
- ``login_config``: the URL + selectors + credential_key the
  most-recent successful ``login`` used. Cached so the auto-retry path
  (re-run ``login`` once on a 401 or login-redirect) doesn't need the
  agent to re-pass them on every request.

Stored as a JSON file at ``<profile-dir>/meta.json``, mode 0600 so
even other code on the sidecar UID can't snoop on it. Reads are
forgiving (missing file ⇒ empty dict, malformed JSON ⇒ empty dict +
warning); writes go through an atomic rename so a crash mid-write
can't leave a half-baked metadata file.

Storing the cached login_config does **not** include credential
material — only the credential_key (the lookup name). Plaintext
username/password live exclusively inside ``credentials.age``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

META_FILENAME = "meta.json"
META_MODE = 0o600


def _meta_path(profile_path: Path) -> Path:
    return profile_path / META_FILENAME


def load_meta(profile_path: Path) -> dict[str, Any]:
    """Return the profile's metadata dict (empty dict if no/bad file)."""
    path = _meta_path(profile_path)
    if not path.exists():
        return {}
    try:
        raw = path.read_text()
    except OSError as e:
        logger.warning("could not read profile meta at %s: %s", path, e)
        return {}
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("profile meta at %s is not valid JSON: %s", path, e)
        return {}
    if not isinstance(decoded, dict):
        logger.warning("profile meta at %s is not a JSON object; ignoring", path)
        return {}
    return decoded


def save_meta(profile_path: Path, meta: dict[str, Any]) -> None:
    """Atomically write the metadata dict back to the profile dir."""
    path = _meta_path(profile_path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    try:
        os.chmod(tmp_path, META_MODE)
    except OSError as e:
        logger.warning("could not chmod profile meta at %s: %s", tmp_path, e)
    tmp_path.replace(path)


def login_enabled(profile_path: Path) -> bool:
    """Return True if this profile has opted into the ``login`` verb."""
    return bool(load_meta(profile_path).get("login_enabled", False))


def get_login_config(profile_path: Path) -> dict[str, Any] | None:
    """Return the cached login config from a previous ``login`` call, or None.

    Used by the auto-retry path on navigate/extract/screenshot: when a
    401 or login-redirect is detected, the runner re-runs ``login``
    with this config (selectors + credential_key + url) once before
    giving up. ``credential_key`` is a lookup string only — the
    plaintext username/password are not in here.
    """
    meta = load_meta(profile_path)
    cfg = meta.get("login_config")
    if not isinstance(cfg, dict):
        return None
    return cfg


def set_login_config(profile_path: Path, config: dict[str, Any]) -> None:
    """Persist ``login_config`` after a successful ``login``.

    The auto-retry path reads this on the next request. Storing the
    URL + selectors + credential_key is safe — none of those are
    secrets. Storing the username or password would be a leak; if a
    caller passes those in by mistake the keys are stripped here as a
    belt-and-braces guard.
    """
    safe = {k: v for k, v in config.items() if k not in {"username", "password"}}
    meta = load_meta(profile_path)
    meta["login_config"] = safe
    save_meta(profile_path, meta)
