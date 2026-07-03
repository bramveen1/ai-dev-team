"""Structured memory index — manifest.json (machine) + INDEX.md (human) + FTS.

Generates a lightweight index of an agent's structured memory files so
retrieval can *search* the memory folder instead of reading all of it
(issue #640). One manifest row per file: canonical key, aliases, one-line
summary, mtime, size. Alongside the manifest, a SQLite FTS5 database
(``search.db``) indexes the *full content* of every file for BM25-ranked
retrieval — stdlib only, no new dependency or service. No vector DB /
embeddings in v1.

The index is regenerated after every persist and every curation, so a
stale index self-heals; readers must tolerate a missing manifest by
falling back to memory.md-only behaviour.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from router.memory_identity import AliasMap
from router.memory_writer import MEMORY_FILE_MODE, write_memory

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"
INDEX_FILENAME = "INDEX.md"
FTS_DB_FILENAME = "search.db"
FTS_TABLE = "memory_fts"
MANIFEST_VERSION = 1
SUMMARY_MAX_CHARS = 140

# bm25() weight per column, in table order: path/category/mtime are
# UNINDEXED (weight ignored); an entity-name hit (key/aliases) counts 3x
# a body-text hit.
FTS_COLUMNS = "path UNINDEXED, category UNINDEXED, key, aliases, content, mtime UNINDEXED"
FTS_BM25_WEIGHTS = (0.0, 0.0, 3.0, 3.0, 1.0, 0.0)

# Structured categories included in the index. memory.md (working set) is
# always loaded whole and is deliberately not indexed.
INDEXED_DIRS = ("people", "projects", "systems", "decisions", "preferences", "lessons", "daily")


def _summarize(text: str) -> str:
    """Return a one-line summary: the first substantive line of the file.

    Skips blank lines and bare markdown headings (a heading like
    ``## 2026-07-01`` carries no content), strips list/emphasis markers,
    and truncates to SUMMARY_MAX_CHARS.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.lstrip("-*").strip().strip("*_")
        if not stripped:
            continue
        if len(stripped) > SUMMARY_MAX_CHARS:
            stripped = stripped[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        return stripped
    return ""


def build_index(memory_path: str | Path, alias_map: AliasMap | None = None) -> dict:
    """Scan the structured memory dirs and write manifest.json + INDEX.md.

    Args:
        memory_path: The agent's memory directory
            (config/agents/{agent}/memory).
        alias_map: Optional alias map used to attach known aliases to each
            entry so retrieval can match on name variants.

    Returns:
        The manifest dict that was written.
    """
    memory_path = Path(memory_path)
    entries: list[dict] = []
    contents: list[str] = []

    for category in INDEXED_DIRS:
        category_dir = memory_path / category
        if not category_dir.is_dir():
            continue
        for md_file in sorted(category_dir.glob("*.md")):
            try:
                stat = md_file.stat()
                content = md_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("Skipping unreadable memory file %s: %s", md_file, e)
                continue
            key = md_file.stem
            aliases = alias_map.aliases_for(category, key) if alias_map else []
            entries.append(
                {
                    "path": f"{category}/{md_file.name}",
                    "category": category,
                    "key": key,
                    "aliases": aliases,
                    "summary": _summarize(content),
                    "mtime": int(stat.st_mtime),
                    "size": stat.st_size,
                }
            )
            contents.append(content)

    manifest = {
        "version": MANIFEST_VERSION,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "files": entries,
    }
    write_memory(memory_path / MANIFEST_FILENAME, json.dumps(manifest, indent=2) + "\n")
    write_memory(memory_path / INDEX_FILENAME, _render_index_md(manifest))
    _build_fts_db(memory_path, entries, contents)
    logger.info("Built memory index at %s: %d files", memory_path, len(entries))
    return manifest


def _build_fts_db(memory_path: Path, entries: list[dict], contents: list[str]) -> None:
    """Rebuild the FTS5 full-text database from scratch, atomically.

    Built at a temp path and renamed over search.db so a concurrent reader
    never sees a half-written database. Failure is non-fatal — the
    retriever falls back to manifest keyword scoring without the db.
    """
    fd, tmp_path = tempfile.mkstemp(dir=memory_path, suffix=".tmp")
    os.close(fd)
    try:
        os.chmod(tmp_path, MEMORY_FILE_MODE)
        # mkstemp created an empty file; sqlite can't init a db over
        # non-sqlite bytes but an empty file is fine.
        conn = sqlite3.connect(tmp_path)
        try:
            conn.execute(f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5({FTS_COLUMNS}, tokenize='porter unicode61')")
            conn.executemany(
                f"INSERT INTO {FTS_TABLE} (path, category, key, aliases, content, mtime) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        entry["path"],
                        entry["category"],
                        entry["key"],
                        " ".join(entry["aliases"]),
                        content,
                        entry["mtime"],
                    )
                    for entry, content in zip(entries, contents)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        os.rename(tmp_path, memory_path / FTS_DB_FILENAME)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.exception("Failed to build FTS index at %s; retrieval will fall back to manifest", memory_path)


def _render_index_md(manifest: dict) -> str:
    """Render the human-readable INDEX.md view of the manifest."""
    lines = [
        "# Memory Index",
        "",
        f"_Generated {manifest['generated']} — {len(manifest['files'])} files. Machine view: `{MANIFEST_FILENAME}`._",
        "",
    ]
    by_category: dict[str, list[dict]] = {}
    for entry in manifest["files"]:
        by_category.setdefault(entry["category"], []).append(entry)
    for category in INDEXED_DIRS:
        rows = by_category.get(category)
        if not rows:
            continue
        lines.append(f"## {category}")
        lines.append("")
        for entry in rows:
            alias_note = f" (aka {', '.join(entry['aliases'])})" if entry["aliases"] else ""
            summary = entry["summary"] or "(empty)"
            lines.append(f"- **{entry['key']}**{alias_note} — {summary} `[{entry['size']} B]`")
        lines.append("")
    return "\n".join(lines)


def load_manifest(memory_path: str | Path) -> dict | None:
    """Load the manifest, or None when missing/unparseable (fallback signal)."""
    manifest_path = Path(memory_path) / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("No manifest at %s", manifest_path)
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load manifest %s: %s", manifest_path, e)
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        logger.warning("Manifest %s has unexpected shape", manifest_path)
        return None
    return manifest


def verify_index(memory_path: str | Path, alias_map: AliasMap | None = None) -> list[str]:
    """Smoke-probe the index. Returns a list of problems (empty = healthy).

    Checks (per issue #640):
      - the manifest parses and references only existing files
      - every known canonical entity resolves to at most one file per category
      - the FTS database opens and holds one row per manifest entry
    """
    memory_path = Path(memory_path)
    problems: list[str] = []

    manifest = load_manifest(memory_path)
    if manifest is None:
        return [f"{MANIFEST_FILENAME} is missing or unparseable"]

    fts_path = memory_path / FTS_DB_FILENAME
    if not fts_path.is_file():
        problems.append(f"{FTS_DB_FILENAME} is missing")
    else:
        try:
            conn = sqlite3.connect(f"file:{fts_path}?mode=ro", uri=True)
            try:
                (row_count,) = conn.execute(f"SELECT count(*) FROM {FTS_TABLE}").fetchone()
            finally:
                conn.close()
            if row_count != len(manifest["files"]):
                problems.append(f"{FTS_DB_FILENAME} has {row_count} rows, manifest has {len(manifest['files'])}")
        except sqlite3.Error as e:
            problems.append(f"{FTS_DB_FILENAME} is unreadable: {e}")

    seen: dict[tuple[str, str], list[str]] = {}
    for entry in manifest["files"]:
        path = entry.get("path", "")
        if not (memory_path / path).is_file():
            problems.append(f"manifest references missing file: {path}")
        category = entry.get("category", "")
        key = entry.get("key", "")
        canonical = alias_map.resolve(category, key) if alias_map else key
        seen.setdefault((category, canonical), []).append(path)

    for (category, canonical), paths in seen.items():
        if len(paths) > 1:
            problems.append(f"entity {category}/{canonical} resolves to {len(paths)} files: {', '.join(paths)}")

    return problems
