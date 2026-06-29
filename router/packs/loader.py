"""Pack loader — discover and parse ``packs/<name>/pack.yaml`` manifests.

A pack lives in a single directory under ``packs/`` (repo-root). The
manifest schema is intentionally tiny — five optional fields plus a
required ``name``:

```yaml
name: github            # required, must match the directory name
description: GitHub access via the gh CLI
needs: [GITHUB_TOKEN]   # secret keys this pack needs at runtime
cli: gh                 # optional: CLI binary to ensure on PATH
approve: [merge]        # optional: verbs that require human approval
```

Files alongside the manifest:

- ``prompt.md``   — appended to the agent's system prompt
- ``mcp.json``    — merged into the agent's ``--mcp-config`` JSON
- ``authenticate.py`` — an ``async def acquire(say)`` helper for grant flow

Everything except ``pack.yaml`` (and the ``name`` field within it) is optional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PACKS_DIR = REPO_ROOT / "packs"


class PackError(Exception):
    """Raised when a pack manifest is missing or malformed."""


@dataclass
class Pack:
    """A loaded pack — its manifest plus paths to optional companion files."""

    name: str
    path: Path
    description: str = ""
    needs: list[str] = field(default_factory=list)
    cli: str | None = None
    approve: list[str] = field(default_factory=list)
    # When True the dispatcher must verify a sidecar service is reachable
    # before invoking the pack. Used by browser_use; defaults to False so
    # existing packs are unaffected.
    requires_sidecar: bool = False
    sidecar_service_name: str | None = None
    sidecar_compose_profile: str | None = None

    @property
    def prompt_path(self) -> Path | None:
        """Path to ``prompt.md`` if it exists, else ``None``."""
        candidate = self.path / "prompt.md"
        return candidate if candidate.exists() else None

    @property
    def mcp_path(self) -> Path | None:
        """Path to ``mcp.json`` if it exists, else ``None``."""
        candidate = self.path / "mcp.json"
        return candidate if candidate.exists() else None

    @property
    def authenticate_path(self) -> Path | None:
        """Path to ``authenticate.py`` if it exists, else ``None``."""
        candidate = self.path / "authenticate.py"
        return candidate if candidate.exists() else None

    @property
    def install_path(self) -> Path | None:
        """Path to ``install.sh`` if it exists, else ``None``.

        The grant flow runs this once after a successful ``authenticate.py``
        to provision any host-side prerequisites (CLI binaries, shared
        volumes). Idempotent re-runs are the pack author's responsibility.
        """
        candidate = self.path / "install.sh"
        return candidate if candidate.exists() else None


def _parse_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise PackError(f"pack manifest not found: {manifest_path}")
    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise PackError(f"failed to parse {manifest_path}: {e}") from e
    if not isinstance(data, dict):
        raise PackError(f"{manifest_path} is not a YAML mapping")
    return data


def load_pack(pack_dir: Path) -> Pack:
    """Load a single pack from its directory.

    The directory must contain ``pack.yaml`` with at least a ``name`` field
    matching the directory name.
    """
    manifest_path = pack_dir / "pack.yaml"
    data = _parse_manifest(manifest_path)

    name = data.get("name")
    if not name:
        raise PackError(f"{manifest_path} missing required 'name' field")
    if name != pack_dir.name:
        raise PackError(f"{manifest_path}: 'name' ({name!r}) does not match directory name ({pack_dir.name!r})")

    needs = data.get("needs") or []
    approve = data.get("approve") or []
    if not isinstance(needs, list):
        raise PackError(f"{manifest_path}: 'needs' must be a list of strings")
    if not isinstance(approve, list):
        raise PackError(f"{manifest_path}: 'approve' must be a list of strings")

    requires_sidecar = bool(data.get("requires_sidecar", False))
    sidecar_service_name = data.get("sidecar_service_name")
    sidecar_compose_profile = data.get("sidecar_compose_profile")
    if requires_sidecar and not sidecar_service_name:
        raise PackError(f"{manifest_path}: 'requires_sidecar: true' must also set 'sidecar_service_name'")

    return Pack(
        name=name,
        path=pack_dir,
        description=data.get("description", ""),
        needs=[str(s) for s in needs],
        cli=data.get("cli"),
        approve=[str(s) for s in approve],
        requires_sidecar=requires_sidecar,
        sidecar_service_name=str(sidecar_service_name) if sidecar_service_name else None,
        sidecar_compose_profile=str(sidecar_compose_profile) if sidecar_compose_profile else None,
    )


def discover_packs(packs_dir: Path | None = None) -> dict[str, Pack]:
    """Discover every pack under ``packs_dir``.

    Skips directories starting with ``_`` (reserved for templates / examples)
    and any directory missing a ``pack.yaml``. Malformed packs are skipped
    with a ``logger.warning`` — one bad manifest does not abort discovery for
    all packs.  :func:`load_pack` still raises :class:`PackError` for direct
    single-pack callers.
    """
    base = packs_dir if packs_dir is not None else DEFAULT_PACKS_DIR
    if not base.exists():
        return {}

    result: dict[str, Pack] = {}
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue
        if not (entry / "pack.yaml").exists():
            continue
        try:
            pack = load_pack(entry)
        except PackError as exc:
            logger.warning("skipping malformed pack %s: %s", entry, exc)
            continue
        result[pack.name] = pack

    return result
