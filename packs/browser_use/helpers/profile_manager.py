"""Per-identity browser profile directories.

Each logical identity (``linkedin-bram``, ``indeed-bram``, …) gets its
own directory under ``/config/browser_profiles/<name>/``. Profiles are
treated as secrets: gitignored, mode 0700, owned by the sidecar UID.
The profile dir holds Chromium's user-data — cookies, IndexedDB,
local storage. Survives sidecar restarts so logged-in sessions stick.

Responsibilities:

- Validate the profile name so we don't escape the parent dir.
- Create the dir with mode 0700 if missing.
- On reuse: refuse if the on-disk mode drifted (mode 0755 or worse),
  because a permissive mode means another container or another user
  on the host may have read the cookies.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from helpers.profile_name import ProfileNameError
from helpers.profile_name import validate_name as _name_validate

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_DIR = Path("/config/browser_profiles")
PROFILES_DIR_ENV = "BROWSER_USE_PROFILES_DIR"
EXPECTED_MODE = 0o700


class ProfileError(RuntimeError):
    """Raised when a profile name is invalid or its dir is in an unsafe state."""


@dataclass(frozen=True)
class Profile:
    """A resolved profile — its name and absolute path."""

    name: str
    path: Path


def resolve_profiles_dir(override: Path | str | None = None) -> Path:
    """Return the profiles root dir (override > env > default)."""
    if override is not None:
        return Path(override)
    env_value = os.environ.get(PROFILES_DIR_ENV)
    if env_value:
        return Path(env_value)
    return DEFAULT_PROFILES_DIR


def validate_name(name: str) -> str:
    """Return ``name`` if it's a valid profile identifier, else raise.

    Thin wrapper around :func:`helpers.profile_name.validate_name` that
    re-raises ``ProfileNameError`` as :class:`ProfileError` so callers
    of this module catch a single exception type for both name-shape
    and FS-state failures.
    """
    try:
        return _name_validate(name)
    except ProfileNameError as e:
        raise ProfileError(str(e)) from e


def ensure_profile(
    name: str,
    *,
    profiles_dir: Path | None = None,
    create_missing: bool = True,
) -> Profile:
    """Return the :class:`Profile` for ``name``, creating its dir if needed.

    - Validates the name shape.
    - Creates the dir with mode 0700 if it doesn't exist and
      ``create_missing`` is True.
    - Refuses to use an existing dir whose mode drifted to anything
      more permissive than 0700.
    """
    validate_name(name)
    base = profiles_dir if profiles_dir is not None else resolve_profiles_dir()
    path = base / name

    if not path.exists():
        if not create_missing:
            raise ProfileError(f"profile {name!r} does not exist at {path}")
        base.mkdir(parents=True, exist_ok=True)
        path.mkdir(mode=EXPECTED_MODE)
        # mkdir's `mode` is umask-masked, so force the bits explicitly
        # in case the parent has a permissive umask.
        os.chmod(path, EXPECTED_MODE)
        logger.info("created browser profile %s at %s", name, path)
        return Profile(name=name, path=path)

    if not path.is_dir():
        raise ProfileError(f"profile path {path} exists but is not a directory")

    mode = path.stat().st_mode & 0o777
    if mode != EXPECTED_MODE:
        raise ProfileError(
            f"profile {name!r} at {path} has mode {mode:#o}; expected "
            f"{EXPECTED_MODE:#o}. Fix with `chmod 0700 {path}` and "
            "audit the dir contents — a permissive mode means another "
            "process may have read the cookies."
        )
    return Profile(name=name, path=path)


def list_profiles(profiles_dir: Path | None = None) -> list[str]:
    """Return profile names present on disk, sorted alphabetically."""
    base = profiles_dir if profiles_dir is not None else resolve_profiles_dir()
    if not base.exists():
        return []
    return sorted(entry.name for entry in base.iterdir() if entry.is_dir())
