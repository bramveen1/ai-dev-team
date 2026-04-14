"""Router memory writer — atomic file writes for memory persistence.

Handles writing and appending to memory files with atomic operations
(write to temp file, then rename) to prevent corruption.
"""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write_memory(path: str | Path, content: str) -> None:
    """Atomically write content to a memory file.

    Creates parent directories if they don't exist. Writes to a temporary
    file in the same directory, then renames to the target path.

    Args:
        path: Target file path.
        content: Content to write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file in same directory, then atomic rename
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp_path, path)
        logger.debug("Wrote memory file %s (%d bytes)", path, len(content))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_memory(path: str | Path, content: str) -> None:
    """Append content to a memory file.

    Creates the file and parent directories if they don't exist.
    Uses atomic write: reads existing content, appends new content,
    then writes the combined result atomically.

    Args:
        path: Target file path.
        content: Content to append.
    """
    path = Path(path)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")

    write_memory(path, existing + content)
    logger.debug("Appended %d bytes to %s", len(content), path)


def persist_memory(
    agent_name: str,
    memory_updates: dict,
    memory_base: str = "/memory",
    agent_base: str = "/agent",
) -> int:
    """Persist structured memory updates to the filesystem.

    Accepts a structured dict of memory updates and writes each category
    to the appropriate file location.

    Args:
        agent_name: Name of the agent whose memory to update.
        memory_updates: Dict with optional keys:
            - decisions: list of {"date": str, "topic": str, "content": str}
            - preferences: list of {"date": str, "content": str}
            - people: list of {"name": str, "context": str}
            - projects: list of {"name": str, "update": str}
            - agent_memory: str to append to agent's memory.md
            - daily_log: str to append to today's daily log
        memory_base: Base path for organizational memory.
        agent_base: Base path for agent memory.

    Returns:
        Number of items persisted.
    """
    import datetime

    today = datetime.date.today().isoformat()
    count = 0
    memory_base_path = Path(memory_base)
    agent_base_path = Path(agent_base)

    # Decisions → /memory/decisions/YYYY-MM-DD.md
    for decision in memory_updates.get("decisions", []):
        date = decision.get("date", today)
        topic = decision.get("topic", "")
        content = decision.get("content", "")
        entry = f"\n## {topic}\n*{date}*\n{content}\n"
        append_memory(memory_base_path / "decisions" / f"{date}.md", entry)
        count += 1
        logger.debug("Persisted decision: %s", topic)

    # Preferences → /memory/preferences/preferences.md
    for pref in memory_updates.get("preferences", []):
        date = pref.get("date", today)
        content = pref.get("content", "")
        entry = f"\n- **{date}** — {content}\n"
        append_memory(memory_base_path / "preferences" / "preferences.md", entry)
        count += 1

    # People → /memory/people/{name}.md
    for person in memory_updates.get("people", []):
        name = person.get("name", "unknown")
        context = person.get("context", "")
        # Sanitize filename
        safe_name = name.lower().replace(" ", "-")
        entry = f"\n## {today}\n{context}\n"
        append_memory(memory_base_path / "people" / f"{safe_name}.md", entry)
        count += 1

    # Projects → /memory/projects/{name}.md
    for project in memory_updates.get("projects", []):
        name = project.get("name", "unknown")
        update = project.get("update", "")
        safe_name = name.lower().replace(" ", "-")
        entry = f"\n## {today}\n{update}\n"
        append_memory(memory_base_path / "projects" / f"{safe_name}.md", entry)
        count += 1

    # Agent memory → /agent/memory.md
    agent_memory = memory_updates.get("agent_memory", "")
    if agent_memory:
        append_memory(agent_base_path / "memory.md", f"\n{agent_memory}\n")
        count += 1

    # Daily log → /memory/daily/YYYY-MM-DD.md
    daily_log = memory_updates.get("daily_log", "")
    if daily_log:
        append_memory(memory_base_path / "daily" / f"{today}.md", f"\n{daily_log}\n")
        count += 1

    logger.info("Persisted %d memory items for agent=%s", count, agent_name)
    return count
