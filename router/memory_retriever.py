"""Memory retrieval — pulls relevant structured memory slices at dispatch.

The missing read path from issue #640: on conversation start the loader
always loads memory.md (working set); this module additionally selects the
relevant structured files — keyed on conversation entities (people
mentioned, active project, recent decisions) — and returns the top-K,
instead of loading everything.

Primary engine: the SQLite FTS5 database (``search.db``) built by
memory_index — BM25-ranked full-content search with porter stemming, entity
names (key/aliases) weighted over body text. Stdlib only; no vector DB /
embeddings in v1. When the FTS db is missing or unreadable, retrieval falls
back to keyword scoring over the manifest; when that is missing too it
degrades to an empty result — memory.md-only, today's behaviour. The whole
path is gated behind MEMORY_RETRIEVAL_ENABLED.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path

from router.memory_index import FTS_BM25_WEIGHTS, FTS_DB_FILENAME, FTS_TABLE, load_manifest

logger = logging.getLogger(__name__)

RETRIEVAL_FLAG_ENV = "MEMORY_RETRIEVAL_ENABLED"

DEFAULT_TOP_K = 5
# Total content budget across retrieved files — keeps the injected slice
# comparable to the working-set cap rather than re-creating load-all.
DEFAULT_MAX_TOTAL_BYTES = 16384

# Manifest-fallback scoring weights: a hit on the entity name (key/alias)
# is a much stronger relevance signal than a word shared with the summary.
KEY_WEIGHT = 3
SUMMARY_WEIGHT = 1

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*[a-z0-9]|[a-z0-9]")

_STOPWORDS = frozenset(
    """a about after all also an and any are as at be been before but by can could did do
    does for from get got had has have he her him his how i if in into is it its just like
    me my no not now of on one or our out over she so some than that the their them then
    there these they this to up us was we were what when where which who why will with would
    you your""".split()
)


def is_retrieval_enabled() -> bool:
    """Return True when the MEMORY_RETRIEVAL_ENABLED env flag is set."""
    return os.environ.get(RETRIEVAL_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens, with hyphen/dot compounds kept whole AND split.

    "ai-dev-team" yields {"ai-dev-team", "ai", "dev", "team"} so a compound
    key matches both the exact slug and its parts mentioned in prose.
    """
    tokens: set[str] = set()
    for match in _TOKEN_RE.findall(text.lower()):
        tokens.add(match)
        tokens.update(part for part in re.split(r"[._-]+", match) if part)
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS}


def _search_fts(memory_path: Path, query_tokens: set[str], limit: int) -> list[str] | None:
    """BM25-rank paths via the FTS5 db; None when the db is missing/unusable.

    Each token is double-quoted in the MATCH expression (the tokenizer only
    emits [a-z0-9._-], so no escaping is needed); FTS5 treats a quoted
    compound like "ai-dev-team" as the phrase ai+dev+team.
    """
    fts_path = memory_path / FTS_DB_FILENAME
    if not fts_path.is_file():
        return None
    match_expr = " OR ".join(f'"{token}"' for token in sorted(query_tokens))
    weights = ", ".join(str(w) for w in FTS_BM25_WEIGHTS)
    try:
        conn = sqlite3.connect(f"file:{fts_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT path FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? "
                f"ORDER BY bm25({FTS_TABLE}, {weights}), mtime DESC LIMIT ?",
                (match_expr, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("FTS search failed on %s (%s); falling back to manifest scoring", fts_path, e)
        return None
    return [row[0] for row in rows]


def _score_entry(entry: dict, query_tokens: set[str]) -> int:
    """Score one manifest entry by keyword overlap with the query."""
    key_tokens = _tokenize(entry.get("key", ""))
    for alias in entry.get("aliases", []):
        key_tokens |= _tokenize(alias)
    summary_tokens = _tokenize(entry.get("summary", ""))

    score = KEY_WEIGHT * len(key_tokens & query_tokens)
    score += SUMMARY_WEIGHT * len((summary_tokens - key_tokens) & query_tokens)
    return score


def _search_manifest(memory_path: Path, query_tokens: set[str]) -> list[str] | None:
    """Fallback ranking from manifest keys/aliases/summaries; None if no manifest."""
    manifest = load_manifest(memory_path)
    if manifest is None:
        return None
    scored: list[tuple[int, int, str]] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            continue
        score = _score_entry(entry, query_tokens)
        if score > 0:
            scored.append((score, entry.get("mtime", 0), entry.get("path", "")))
    # Highest score first; recency breaks ties.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _score, _mtime, path in scored]


def retrieve_relevant_memory(
    agent_name: str,
    query_text: str,
    agent_base: str | Path = "/config/agents",
    top_k: int = DEFAULT_TOP_K,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[tuple[str, str]]:
    """Select the structured memory files relevant to a conversation.

    Args:
        agent_name: The agent whose memory to search.
        query_text: Text to key retrieval on (the new message, typically).
        agent_base: Base path for agent directories.
        top_k: Maximum number of files to return.
        max_total_bytes: Total content budget across returned files.

    Returns:
        A list of (relative path, content) tuples ordered by relevance,
        empty when the index is missing or nothing matches.
    """
    memory_path = Path(agent_base) / agent_name / "memory"
    query_tokens = _tokenize(query_text or "")
    if not query_tokens:
        return []

    # Over-fetch so unreadable files don't shrink the result below top_k.
    ranked = _search_fts(memory_path, query_tokens, limit=top_k * 3)
    engine = "fts5"
    if ranked is None:
        ranked = _search_manifest(memory_path, query_tokens)
        engine = "manifest"
    if ranked is None:
        return []

    results: list[tuple[str, str]] = []
    total_bytes = 0
    for rel_path in ranked:
        if len(results) >= top_k:
            break
        target = (memory_path / rel_path).resolve()
        try:
            target.relative_to(memory_path.resolve())
        except ValueError:
            logger.error("Indexed path %r escapes the memory dir; skipping", rel_path)
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Retrieval skipping unreadable %s: %s", rel_path, e)
            continue
        if results and total_bytes + len(content) > max_total_bytes:
            break
        results.append((rel_path, content))
        total_bytes += len(content)

    logger.info(
        "Memory retrieval for agent=%s via %s: %d files matched, returning %d (%d bytes)",
        agent_name,
        engine,
        len(ranked),
        len(results),
        total_bytes,
    )
    return results
