"""Profile name validation — pure regex, no filesystem access.

Lives outside ``profile_manager`` so the agent-side handler can
validate names locally without importing the module that owns the
on-disk profile tree. The sidecar (which owns mode 0700 cookie dirs
at UID:GID different from the agent) is the only thing that should
``stat``/``mkdir`` under ``/config/browser_profiles``; touching that
tree from the agent UID would either fail with ``PermissionError`` or
defeat the security boundary.

``profile_manager.ensure_profile`` reuses the same regex by importing
``validate_name`` from here, so the two callers can't drift.
"""

from __future__ import annotations

import re

# Logical identity names — lowercase, alphanumerics, dashes, single
# underscores. No dots, no slashes, no leading dashes. The router is
# the source of truth for profile names; we mirror its convention so
# typos surface here rather than as a silent mkdir somewhere weird.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


class ProfileNameError(ValueError):
    """Raised when a profile name fails the format check."""


def validate_name(name: str) -> str:
    """Return ``name`` if it matches the profile-name format, else raise."""
    if not _NAME_RE.match(name or ""):
        raise ProfileNameError(
            f"invalid profile name {name!r}: expected lowercase "
            "alphanumerics, dashes, underscores; no leading/trailing "
            "separators; length 1–64"
        )
    return name
