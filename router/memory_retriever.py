"""Memory retrieval — pulls relevant structured memory slices at dispatch.

The missing read path from issue #640: on conversation start the loader
always loads memory.md (working set); this module additionally selects the
relevant structured files via the manifest index — keyed on conversation
entities (people mentioned, active project, recent decisions) — and returns
the top-K, instead of loading everything.

Deliberately simple: keyword overlap against the manifest's canonical keys,
aliases, and one-line summaries. No vector DB / embeddings in v1. A missing
manifest or any error degrades to an empty result — memory.md-only, today's
behaviour. The whole path is gated behind MEMORY_RETRIEVAL_ENABLED.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from router.memory_index import load_manifest

logger = logging.getLogger(__name__)

RETRIEVAL_FLAG_ENV = "MEMORY_RETRIEVAL_ENABLED"

DEFAULT_TOP_K = 5
# Total content budget across retrieved files — keeps the injected slice
# comparable to the working-set cap rather than re-creating load-all.
DEFAULT_MAX_TOTAL_BYTES = 16384

# Scoring weights: a hit on the entity name (key/alias) is a much stronger
# relevance signal than a word shared with the summary line.
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


def _score_entry(entry: dict, query_tokens: set[str]) -> int:
    """Score one manifest entry by keyword overlap with the query."""
    key_tokens = _tokenize(entry.get("key", ""))
    for alias in entry.get("aliases", []):
        key_tokens |= _tokenize(alias)
    summary_tokens = _tokenize(entry.get("summary", ""))

    score = KEY_WEIGHT * len(key_tokens & query_tokens)
    score += SUMMARY_WEIGHT * len((summary_tokens - key_tokens) & query_tokens)
    return score


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
    manifest = load_manifest(memory_path)
    if manifest is None:
        return []

    query_tokens = _tokenize(query_text or "")
    if not query_tokens:
        return []

    scored: list[tuple[int, int, dict]] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            continue
        score = _score_entry(entry, query_tokens)
        if score > 0:
            scored.append((score, entry.get("mtime", 0), entry))
    # Highest score first; recency breaks ties.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    results: list[tuple[str, str]] = []
    total_bytes = 0
    for _score, _mtime, entry in scored:
        if len(results) >= top_k:
            break
        rel_path = entry.get("path", "")
        target = (memory_path / rel_path).resolve()
        try:
            target.relative_to(memory_path.resolve())
        except ValueError:
            logger.error("Manifest path %r escapes the memory dir; skipping", rel_path)
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
        "Memory retrieval for agent=%s: %d/%d indexed files matched, returning %d (%d bytes)",
        agent_name,
        len(scored),
        len(manifest["files"]),
        len(results),
        total_bytes,
    )
    return results
