"""Router memory loader — reads memory files from disk for agent context.

Loads organizational memory, agent-specific memory, and system documentation
files. Handles missing files gracefully and tracks loaded context sizes.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_memory(path: str | Path) -> str:
    """Load a single memory file from disk.

    Args:
        path: Path to the memory file.

    Returns:
        File content as a string, or empty string if the file is missing.
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
        logger.debug("Loaded memory file %s (%d bytes)", path, len(content))
        return content
    except (FileNotFoundError, OSError) as e:
        logger.warning("Could not load memory file %s: %s", path, e)
        return ""


def get_memory_size(path: str | Path) -> int:
    """Return the size of a memory file in bytes.

    Args:
        path: Path to the memory file.

    Returns:
        File size in bytes, or 0 if the file is missing.
    """
    path = Path(path)
    try:
        return path.stat().st_size
    except (FileNotFoundError, OSError):
        return 0


def load_all_memory(directory: str | Path) -> dict[str, str]:
    """Load all markdown files from a directory tree.

    Args:
        directory: Root directory to scan for .md files.

    Returns:
        A dict mapping relative file paths (as strings) to their content.
    """
    directory = Path(directory)
    result: dict[str, str] = {}

    if not directory.is_dir():
        logger.warning("Memory directory %s does not exist", directory)
        return result

    for md_file in sorted(directory.rglob("*.md")):
        if md_file.is_file():
            rel_path = str(md_file.relative_to(directory))
            content = load_memory(md_file)
            if content:
                result[rel_path] = content

    total_bytes = sum(len(v) for v in result.values())
    logger.info("Loaded %d memory files from %s (total %d bytes)", len(result), directory, total_bytes)
    return result


def load_agent_memory(
    agent_name: str,
    memory_base: str = "/memory",
    agent_base: str = "/agent",
    systems_base: str = "/systems",
    agent_tools: dict | None = None,
) -> dict:
    """Load all memory context for a specific agent.

    Reads organizational memory, agent-specific memory, and relevant system
    documentation files based on the agent's tool configuration.

    Args:
        agent_name: Logical name of the agent (e.g. "lisa").
        memory_base: Base path for organizational memory (mounted volume).
        agent_base: Base path for agent memory (mounted volume).
        systems_base: Base path for system documentation files.
        agent_tools: Optional dict mapping agent names to lists of system doc filenames.
            If None, no system docs are loaded.

    Returns:
        A dict with keys:
            - org_memory: Contents of MEMORY.md
            - agent_memory: Contents of the agent's memory.md
            - system_docs: List of system doc content strings
    """
    org_memory = load_memory(Path(memory_base) / "MEMORY.md")
    agent_memory = load_memory(Path(agent_base) / "memory.md")

    system_docs: list[str] = []
    if agent_tools and agent_name in agent_tools:
        for doc_filename in agent_tools[agent_name]:
            doc_path = Path(systems_base) / doc_filename
            content = load_memory(doc_path)
            if content:
                system_docs.append(content)

    total_size = len(org_memory) + len(agent_memory) + sum(len(d) for d in system_docs)
    logger.info(
        "Loaded memory for agent=%s: org=%d bytes, agent=%d bytes, systems=%d files (%d bytes), total=%d bytes",
        agent_name,
        len(org_memory),
        len(agent_memory),
        len(system_docs),
        sum(len(d) for d in system_docs),
        total_size,
    )

    return {
        "org_memory": org_memory,
        "agent_memory": agent_memory,
        "system_docs": system_docs,
    }


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
