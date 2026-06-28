"""Router memory writer — atomic file writes for memory persistence.

Handles writing and appending to memory files with atomic operations
(write to temp file, then rename) to prevent corruption.
"""

import asyncio
import datetime
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

WORKING_MEMORY_MAX_BYTES = 3072  # 3KB soft cap for memory.md

# Common LLM date formats tried in order before falling back to today.
_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%m/%d/%Y",
]


def _normalize_date(date_str: str, today: str) -> str:
    """Return a safe ISO YYYY-MM-DD string from an LLM-supplied date.

    Tries to parse *date_str* and reformat it to ISO.  Any unparseable or
    empty value falls back to *today*.  The result is always a plain
    YYYY-MM-DD token with no path separators.
    """
    stripped = (date_str or "").strip()
    if not stripped:
        return today

    # Fast path: already a valid ISO date.
    try:
        return datetime.date.fromisoformat(stripped).isoformat()
    except ValueError:
        pass

    # Try common non-ISO formats produced by LLMs.
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(stripped, fmt).date().isoformat()
        except ValueError:
            continue

    logger.warning("Unrecognised decision date %r; falling back to today (%s)", date_str, today)
    return today


_agent_locks: dict[str, asyncio.Lock] = {}


def get_agent_lock(agent_name: str) -> asyncio.Lock:
    """Return (creating if necessary) the per-agent asyncio.Lock for memory.md writes.

    Using one lock per agent means concurrent writes for different agents
    do not block each other, while writes for the same agent are serialised.
    """
    if agent_name not in _agent_locks:
        _agent_locks[agent_name] = asyncio.Lock()
    return _agent_locks[agent_name]


# Memory files hold conversation context that other users on the host
# shouldn't see. The router and agent containers both run as uid 1000,
# so 0600/0700 is enough to protect the data without breaking access.
MEMORY_FILE_MODE = 0o600
MEMORY_DIR_MODE = 0o700


def _ensure_memory_dir(path: Path) -> None:
    """Create the directory if missing and tighten its mode to 0700.

    ``Path.mkdir(mode=..., parents=True)`` only applies the mode to the
    leaf in Python's pathlib — parents pick up the umask-modified default
    (typically 0755). For the memory tree we want every level to be 0700
    so a future co-tenant can't ``ls`` an agent's notes.
    """
    path.mkdir(mode=MEMORY_DIR_MODE, parents=True, exist_ok=True)
    try:
        path.chmod(MEMORY_DIR_MODE)
    except OSError:
        # Best-effort: the directory may be on a filesystem that
        # doesn't honour chmod (CI tmpfs, NFS no_root_squash, …).
        pass


def write_memory(path: str | Path, content: str) -> None:
    """Atomically write content to a memory file.

    Creates parent directories if they don't exist. Writes to a temporary
    file in the same directory, then renames to the target path.

    Args:
        path: Target file path.
        content: Content to write.
    """
    path = Path(path)
    _ensure_memory_dir(path.parent)

    # Write to temp file in same directory, then atomic rename
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.chmod(tmp_path, MEMORY_FILE_MODE)
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


async def persist_memory(
    agent_name: str,
    memory_updates: dict,
    agent_base: str = "/config/agents",
) -> int:
    """Persist structured memory updates to the filesystem.

    All memory is written per-agent under config/agents/{agent}/memory/.

    Args:
        agent_name: Name of the agent whose memory to update.
        memory_updates: Dict with optional keys:
            - decisions: list of {"date": str, "topic": str, "content": str}
            - preferences: list of {"date": str, "content": str}
            - people: list of {"name": str, "context": str}
            - projects: list of {"name": str, "update": str}
            - agent_memory: str to append to agent's memory.md
            - daily_log: str to append to today's daily log
        agent_base: Base path for agent directories.

    Returns:
        Number of items persisted.
    """
    today = datetime.date.today().isoformat()
    count = 0
    memory_path = Path(agent_base) / agent_name / "memory"

    # Decisions → config/agents/{agent}/memory/decisions/YYYY-MM-DD.md
    for decision in memory_updates.get("decisions", []):
        raw_date = decision.get("date", today)
        date = _normalize_date(raw_date, today)
        # os.path.basename prevents a '/' in the resolved date from escaping decisions/
        safe_filename = os.path.basename(f"{date}.md")
        topic = decision.get("topic", "")
        content = decision.get("content", "")
        entry = f"\n## {topic}\n*{date}*\n{content}\n"
        append_memory(memory_path / "decisions" / safe_filename, entry)
        count += 1
        logger.debug("Persisted decision: %s", topic)

    # Preferences → config/agents/{agent}/memory/preferences/preferences.md
    for pref in memory_updates.get("preferences", []):
        date = pref.get("date", today)
        content = pref.get("content", "")
        entry = f"\n- **{date}** — {content}\n"
        append_memory(memory_path / "preferences" / "preferences.md", entry)
        count += 1

    # People → config/agents/{agent}/memory/people/{name}.md
    for person in memory_updates.get("people", []):
        name = person.get("name", "unknown")
        context = person.get("context", "")
        safe_name = name.lower().replace(" ", "-")
        entry = f"\n## {today}\n{context}\n"
        append_memory(memory_path / "people" / f"{safe_name}.md", entry)
        count += 1

    # Projects → config/agents/{agent}/memory/projects/{name}.md
    for project in memory_updates.get("projects", []):
        name = project.get("name", "unknown")
        update = project.get("update", "")
        safe_name = name.lower().replace(" ", "-")
        entry = f"\n## {today}\n{update}\n"
        append_memory(memory_path / "projects" / f"{safe_name}.md", entry)
        count += 1

    # Agent memory → config/agents/{agent}/memory/memory.md
    # Hold the per-agent lock so a concurrent curation write cannot overwrite
    # an append that lands during the curator's long CLI await.
    agent_memory = memory_updates.get("agent_memory", "")
    if agent_memory:
        memory_md_path = memory_path / "memory.md"
        async with get_agent_lock(agent_name):
            append_memory(memory_md_path, f"\n{agent_memory}\n")
        count += 1
        try:
            size = memory_md_path.stat().st_size
            if size > WORKING_MEMORY_MAX_BYTES:
                logger.warning(
                    "Working memory for %s is %d bytes (cap: %d). Needs curation.",
                    agent_name,
                    size,
                    WORKING_MEMORY_MAX_BYTES,
                )
        except OSError:
            pass

    # Daily log → config/agents/{agent}/memory/daily/YYYY-MM-DD.md
    daily_log = memory_updates.get("daily_log", "")
    if daily_log:
        append_memory(memory_path / "daily" / f"{today}.md", f"\n{daily_log}\n")
        count += 1

    logger.info("Persisted %d memory items for agent=%s", count, agent_name)
    return count
