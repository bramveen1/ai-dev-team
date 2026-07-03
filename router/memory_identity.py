"""Canonical identity resolution for structured memory files.

Maps LLM-supplied person/project/system name variants to one canonical
slug via an explicit, reviewable alias map, so the memory writer merges
updates into a single canonical file instead of minting a new file per
name variant (issue #640).

The alias map is shared org-wide (identities are org-wide) and lives at
``/config/shared/memory-aliases.json`` by default:

.. code-block:: json

    {
      "people": {
        "bram": ["Bram Veenhof", "bramveen1", "bramveenhof@gmail.com"]
      },
      "projects": {
        "ai-dev-team": ["ai dev team", "aidevteam router"]
      },
      "systems": {}
    }

Resolution is exact (after sanitization) — unknown names keep their own
sanitized slug and get their own file. We never merge on a guess.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ALIAS_MAP_PATH = "/config/shared/memory-aliases.json"
ALIAS_MAP_ENV_VAR = "MEMORY_ALIAS_MAP_PATH"

# Categories that key files on an entity name and therefore fragment
# without canonical identity. Dated categories (daily/, decisions/) are
# keyed on the date and don't need aliasing.
ALIAS_CATEGORIES = ("people", "projects", "systems")


def sanitize_name(name: str) -> str:
    """Sanitize an LLM-supplied entity name to a safe filesystem leaf.

    Replaces every character outside [a-z0-9._-] with a hyphen, then strips
    leading dots (so '..' cannot survive) and surrounding hyphens.  An empty
    or all-separator result falls back to 'unknown'.
    """
    safe = re.sub(r"[^a-z0-9._-]", "-", name.lower())
    safe = safe.lstrip(".")  # prevent '..', hidden filenames
    safe = safe.strip("-")
    return safe or "unknown"


class AliasMap:
    """Reverse index from sanitized name variants to canonical slugs."""

    def __init__(self, raw: dict | None = None):
        # category -> {sanitized alias -> canonical slug}
        self._reverse: dict[str, dict[str, str]] = {cat: {} for cat in ALIAS_CATEGORIES}
        # category -> {canonical slug -> [original alias strings]}
        self._aliases: dict[str, dict[str, list[str]]] = {cat: {} for cat in ALIAS_CATEGORIES}
        if not raw:
            return
        for category in ALIAS_CATEGORIES:
            section = raw.get(category)
            if not isinstance(section, dict):
                continue
            for canonical, aliases in section.items():
                canonical_slug = sanitize_name(str(canonical))
                self._aliases[category][canonical_slug] = []
                # The canonical slug always resolves to itself.
                self._reverse[category][canonical_slug] = canonical_slug
                if not isinstance(aliases, list):
                    logger.warning(
                        "Alias map entry %s/%s is not a list; ignoring its aliases",
                        category,
                        canonical,
                    )
                    continue
                for alias in aliases:
                    alias_slug = sanitize_name(str(alias))
                    existing = self._reverse[category].get(alias_slug)
                    if existing is not None and existing != canonical_slug:
                        logger.warning(
                            "Alias %r maps to both %r and %r in %s; keeping %r",
                            alias,
                            existing,
                            canonical_slug,
                            category,
                            existing,
                        )
                        continue
                    self._reverse[category][alias_slug] = canonical_slug
                    self._aliases[category][canonical_slug].append(str(alias))

    def resolve(self, category: str, name: str) -> str:
        """Resolve a raw entity name to its canonical filesystem slug.

        Unknown names (or unknown categories) fall back to their own
        sanitized slug — never merged on a guess.
        """
        slug = sanitize_name(name)
        return self._reverse.get(category, {}).get(slug, slug)

    def aliases_for(self, category: str, canonical: str) -> list[str]:
        """Return the known alias strings for a canonical slug (may be empty)."""
        return list(self._aliases.get(category, {}).get(canonical, []))

    def canonical_keys(self, category: str) -> list[str]:
        """Return all canonical slugs declared for a category."""
        return list(self._aliases.get(category, {}).keys())


def alias_map_path() -> Path:
    """Return the alias map path (env override or the shared default)."""
    return Path(os.environ.get(ALIAS_MAP_ENV_VAR, DEFAULT_ALIAS_MAP_PATH))


def load_alias_map(path: str | Path | None = None) -> AliasMap:
    """Load the alias map from disk.

    A missing or unparseable file degrades to an empty map — resolution
    then behaves exactly like plain sanitization (today's behaviour).
    """
    path = Path(path) if path is not None else alias_map_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("No alias map at %s; using empty map", path)
        return AliasMap()
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load alias map %s: %s; using empty map", path, e)
        return AliasMap()
    if not isinstance(raw, dict):
        logger.warning("Alias map %s is not a JSON object; using empty map", path)
        return AliasMap()
    return AliasMap(raw)
