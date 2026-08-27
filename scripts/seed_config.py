"""Seed config/ from config.example/, syncing tracked defaults on every run.

``make seed-config`` used to be a plain ``cp -rn config.example/. config/``
(no-clobber). That meant once a host was seeded, updated defaults shipped in
tracked ``config.example/`` files were silently never applied again — the
deploy reported success while the host kept running stale config (#378).

This splits that single copy into two tiers:

* "seed once" paths — host-specific secrets/identity that get mutated at
  runtime (grants, curated memory, real credentials) and must never be
  clobbered once a host has customised them. Copied only if missing.
* everything else — tracked defaults (role/persona templates, shared
  worldview, settings), synced (overwritten when changed) on every run so
  updates in ``config.example/`` actually reach an already-seeded host.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

# Relative to the config root. Copied once (skipped if the destination
# already exists) — never overwritten by a later seed run. Mirrors the
# "files explicitly NOT touched on prod" list in scripts/sync-config-to-prod.sh.
SEED_ONCE_RELPATHS = {
    "shared/MEMORY.md",  # host-curated org memory, not the shipped template
}


def _is_seed_once(rel_path: Path) -> bool:
    if str(rel_path) in SEED_ONCE_RELPATHS:
        return True
    parts = rel_path.parts
    if "secrets" in parts:  # real credentials once populated
        return True
    if parts and parts[-1] == "agent.yaml":  # mutated at runtime by grant/revoke
        return True
    if "memory" in parts:  # per-agent runtime memory
        return True
    return False


def seed_config(example_dir: Path, config_dir: Path) -> tuple[list[str], list[str]]:
    """Copy *example_dir* into *config_dir*.

    Returns ``(synced, preserved)`` — relative paths written/updated and
    relative paths left alone because they're seed-once and already exist.
    """
    synced: list[str] = []
    preserved: list[str] = []

    for src in sorted(p for p in example_dir.rglob("*") if p.is_file()):
        rel = src.relative_to(example_dir)
        dest = config_dir / rel

        if _is_seed_once(rel):
            if dest.exists():
                preserved.append(str(rel))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            synced.append(str(rel))
            continue

        if dest.exists() and filecmp.cmp(src, dest, shallow=False):
            continue  # already up to date
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        synced.append(str(rel))

    return synced, preserved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example-dir", type=Path, default=Path("config.example"))
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    args = parser.parse_args()

    args.config_dir.mkdir(parents=True, exist_ok=True)
    synced, preserved = seed_config(args.example_dir, args.config_dir)

    for rel in synced:
        print(f"synced:    {rel}")
    for rel in preserved:
        print(f"preserved: {rel} (host-specific, seeded once)")
    print(f"config/ seeded from config.example/ ({len(synced)} synced, {len(preserved)} preserved)")


if __name__ == "__main__":
    main()
