"""Memory loader — reads memory and context files from disk.

Provides functions to load individual memory files, track file sizes,
and bulk-load all memory from a directory tree. Used by the dispatcher
to assemble agent context at session start.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_memory(path: str | Path) -> str:
    """Load a single memory file and return its content as a string.

    Args:
        path: Path to the memory file.

    Returns:
        The file content as a string, or empty string if the file
        does not exist or cannot be read.
    """
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read memory file %s: %s", path, e)
        return ""


def get_memory_size(path: str | Path) -> int:
    """Return the size of a memory file in bytes.

    Args:
        path: Path to the memory file.

    Returns:
        File size in bytes, or 0 if the file does not exist.
    """
    try:
        p = Path(path)
        if not p.is_file():
            return 0
        return p.stat().st_size
    except OSError:
        return 0


def load_all_memory(directory: str | Path) -> dict[str, str]:
    """Load all markdown files from a directory tree.

    Recursively finds all ``.md`` files under the given directory
    and returns a dict mapping each file's path (as string) to its content.

    Args:
        directory: Root directory to scan.

    Returns:
        A dict mapping file path strings to file content strings.
    """
    result: dict[str, str] = {}
    d = Path(directory)
    if not d.is_dir():
        return result

    for md_file in sorted(d.rglob("*.md")):
        if md_file.is_file():
            try:
                result[str(md_file)] = md_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("Failed to read %s: %s", md_file, e)
    return result


def load_agent_context(
    agent_name: str,
    memory_dir: str | Path,
    agent_dir: str | Path,
) -> list[tuple[str, str]]:
    """Load all context files for an agent in the correct order.

    Returns a list of (label, content) tuples in loading order:
    1. memory/shared/SOUL.md — universal behavior rules
    2. agents/{agent}/role.md — job description and responsibilities
    3. memory/{agent}/personality.md — agent-specific voice
    4. agents/{agent}/memory.md — agent-specific accumulated knowledge
    5. memory/MEMORY.md — org-wide context index

    Args:
        agent_name: The agent's name (e.g. "lisa").
        memory_dir: Path to the shared memory directory.
        agent_dir: Path to the agent's directory (e.g. agents/lisa/).

    Returns:
        A list of (label, content) tuples. Empty-content files are omitted.
    """
    memory_dir = Path(memory_dir)
    agent_dir = Path(agent_dir)

    files = [
        ("soul", memory_dir / "shared" / "SOUL.md"),
        ("role", agent_dir / "role.md"),
        ("personality", memory_dir / agent_name / "personality.md"),
        ("agent_memory", agent_dir / "memory.md"),
        ("org_memory", memory_dir / "MEMORY.md"),
    ]

    context: list[tuple[str, str]] = []
    for label, path in files:
        content = load_memory(path)
        if content.strip():
            context.append((label, content))
            logger.debug("Loaded %s from %s (%d bytes)", label, path, len(content))
        else:
            logger.debug("Skipped %s (empty or missing: %s)", label, path)

    return context
