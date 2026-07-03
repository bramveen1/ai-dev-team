"""One-time structured-memory migration for issue #640.

Dedups slug-variant entity files into their canonical file (per the shared
alias map), archives cruft (salvage-* trees, *.bak* files), and builds the
initial manifest index for each agent.

Reversible by design: nothing is ever deleted — merged and cruft files are
moved under ``_archive/`` inside each agent's memory directory, and merged
content is appended to the canonical file with a provenance marker.

Dry-run by default; pass --apply to actually migrate.

Usage:
    python -m scripts.migrate_memory [--agent-base /config/agents]
                                     [--alias-map /config/shared/memory-aliases.json]
                                     [--agent lisa] [--apply]
"""

from __future__ import annotations

import argparse
import datetime
import logging
import shutil
import sys
from pathlib import Path

from router.memory_identity import ALIAS_CATEGORIES, AliasMap, load_alias_map
from router.memory_index import build_index
from router.memory_writer import append_memory

logger = logging.getLogger(__name__)

ARCHIVE_DIR = "_archive"


def _archive_target(archive_root: Path, rel_name: str) -> Path:
    """Return a collision-free path under the archive root for rel_name."""
    target = archive_root / rel_name
    counter = 1
    while target.exists():
        target = archive_root / f"{rel_name}.{counter}"
        counter += 1
    return target


def _move_to_archive(path: Path, archive_root: Path, rel_name: str, apply: bool, actions: list[str]) -> None:
    actions.append(f"archive {path.name} -> {ARCHIVE_DIR}/{rel_name}")
    if apply:
        archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = _archive_target(archive_root, rel_name)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(path), str(target))


def _archive_cruft(memory_path: Path, apply: bool, actions: list[str]) -> None:
    """Move salvage-* trees and *.bak* files to _archive/."""
    archive_root = memory_path / ARCHIVE_DIR
    for child in sorted(memory_path.iterdir()):
        if child.name == ARCHIVE_DIR:
            continue
        if child.name.startswith("salvage-") or ".bak" in child.name:
            _move_to_archive(child, archive_root, child.name, apply, actions)


def _merge_duplicates(memory_path: Path, alias_map: AliasMap, apply: bool, actions: list[str]) -> None:
    """Merge slug-variant files into their canonical file, archive originals."""
    today = datetime.date.today().isoformat()
    archive_root = memory_path / ARCHIVE_DIR
    for category in ALIAS_CATEGORIES:
        category_dir = memory_path / category
        if not category_dir.is_dir():
            continue
        for md_file in sorted(category_dir.glob("*.md")):
            canonical = alias_map.resolve(category, md_file.stem)
            if canonical == md_file.stem:
                continue
            canonical_path = category_dir / f"{canonical}.md"
            actions.append(f"merge {category}/{md_file.name} -> {category}/{canonical}.md")
            if apply:
                content = md_file.read_text(encoding="utf-8")
                header = f"\n<!-- merged from {category}/{md_file.name} on {today} (#640 migration) -->\n"
                append_memory(canonical_path, header + content)
            _move_to_archive(md_file, archive_root, f"{category}/{md_file.name}", apply, actions)


def migrate_agent_memory(memory_path: Path, alias_map: AliasMap, apply: bool) -> list[str]:
    """Migrate one agent's memory directory. Returns the action log."""
    actions: list[str] = []
    _archive_cruft(memory_path, apply, actions)
    _merge_duplicates(memory_path, alias_map, apply, actions)
    if apply:
        build_index(memory_path, alias_map)
        actions.append("rebuilt index (manifest.json + INDEX.md)")
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-base", default="/config/agents", help="Base path for agent directories")
    parser.add_argument("--alias-map", default=None, help="Path to memory-aliases.json (default: shared location)")
    parser.add_argument("--agent", default=None, help="Migrate only this agent (default: all)")
    parser.add_argument("--apply", action="store_true", help="Actually migrate (default: dry-run)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    alias_map = load_alias_map(args.alias_map)
    agent_base = Path(args.agent_base)
    if not agent_base.is_dir():
        logger.error("Agent base %s does not exist", agent_base)
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    total_actions = 0
    for agent_dir in sorted(agent_base.iterdir()):
        if args.agent and agent_dir.name != args.agent:
            continue
        memory_path = agent_dir / "memory"
        if not memory_path.is_dir():
            continue
        actions = migrate_agent_memory(memory_path, alias_map, args.apply)
        total_actions += len(actions)
        print(f"[{mode}] {agent_dir.name}: {len(actions)} actions")
        for action in actions:
            print(f"  - {action}")

    if not args.apply and total_actions:
        print(f"\n{total_actions} actions planned. Re-run with --apply to migrate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
