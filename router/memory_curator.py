"""Daily memory curation — promotes important items to working memory.

Runs in the background on the first message of each day. Reads only
new long-term memory entries since the last curation, merges highlights
into the existing working memory, and keeps it under the size cap.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from router.container_exec import run_in_container as _run_in_container
from router.memory_identity import load_alias_map
from router.memory_index import build_index, verify_index
from router.memory_writer import (
    MEMORY_FILE_MODE,
    WORKING_MEMORY_MAX_BYTES,
    _ensure_memory_dir,
    get_agent_lock,
    write_memory,
)

logger = logging.getLogger(__name__)

TREND_LOOKBACK_DAYS = 5
MARKER_FILENAME = ".last_curated"

# Module-level in-flight guard — keyed by agent_name.
# Prevents multiple concurrent curation tasks for the same agent during the
# ~120 s window before _write_marker is called (issue #511).
_curation_in_flight: set[str] = set()


def is_curation_in_flight(agent_name: str) -> bool:
    """Return True if a curation task is currently running for agent_name."""
    return agent_name in _curation_in_flight


CURATION_PROMPT = """\
You are curating an agent's working memory. Your job is to merge new entries \
into the existing working memory, keeping it concise and high-value.

## Current working memory
{current_memory}

## New entries since last curation
{new_entries}

## Recent context (last {trend_days} days, for trend awareness)
{trend_context}

## Instructions
Rewrite the working memory as a curated summary:
- Maximum {max_bytes} bytes (~{max_tokens} tokens)
- Sections: ## Key People, ## Active Projects, ## Systems, ## Recent Decisions, ## Preferences, ## Notes
- Prioritise: active projects, key people context, recent decisions, strong preferences
- Drop stale items (completed tasks, outdated status)
- Note emerging trends or patterns from recent context
- Each entry: 1-2 sentences, include WHY it matters
- Output ONLY the new memory.md content — no preamble, no explanation\
"""


def needs_curation(agent_name: str, agent_base: str = "/config/agents") -> bool:
    """Check whether an agent's memory needs curation today.

    Returns True if the .last_curated marker is missing or before today.
    """
    marker = Path(agent_base) / agent_name / "memory" / MARKER_FILENAME
    if not marker.exists():
        return True
    try:
        marker_date = marker.read_text(encoding="utf-8").strip()
        return marker_date != datetime.date.today().isoformat()
    except (OSError, ValueError):
        return True


async def curate_agent_memory(
    agent_name: str,
    container: str,
    agent_base: str = "/config/agents",
    timeout: int = 120,
) -> bool:
    """Run incremental curation for one agent's memory.

    Reads only entries newer than .last_curated, merges them into the
    existing working memory, and writes the updated result.

    Args:
        agent_name: The agent's name (e.g. "lisa").
        container: Docker container name.
        agent_base: Base path for agent directories.
        timeout: CLI invocation timeout in seconds.

    Returns:
        True if curation succeeded, False otherwise.
    """
    if agent_name in _curation_in_flight:
        logger.info("Curation already in flight for %s, skipping", agent_name)
        return False
    _curation_in_flight.add(agent_name)
    try:
        return await _do_curate_agent_memory(agent_name, container, agent_base, timeout)
    finally:
        _curation_in_flight.discard(agent_name)


async def _do_curate_agent_memory(
    agent_name: str,
    container: str,
    agent_base: str,
    timeout: int,
) -> bool:
    """Inner curation logic — called only when the in-flight guard is held."""
    memory_path = Path(agent_base) / agent_name / "memory"
    marker_path = memory_path / MARKER_FILENAME
    today = datetime.date.today()

    # Determine what's new
    since_date = _get_last_curated_date(marker_path)

    # Load current working memory
    current_memory = _read_file(memory_path / "memory.md")

    # Collect new entries since last curation
    new_entries = _collect_new_entries(memory_path, since_date)
    if not new_entries and current_memory:
        # Nothing new to curate — just update the marker
        _write_marker(marker_path, today)
        logger.info("No new entries to curate for %s", agent_name)
        return True

    # Collect trend context (last N days for pattern recognition)
    trend_start = today - datetime.timedelta(days=TREND_LOOKBACK_DAYS)
    trend_context = _collect_trend_context(memory_path, trend_start, today)

    max_tokens = WORKING_MEMORY_MAX_BYTES // 4
    prompt = CURATION_PROMPT.format(
        current_memory=current_memory or "(empty — first curation)",
        new_entries=new_entries or "(none)",
        trend_context=trend_context or "(none)",
        trend_days=TREND_LOOKBACK_DAYS,
        max_bytes=WORKING_MEMORY_MAX_BYTES,
        max_tokens=max_tokens,
    )

    cli_cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--max-turns",
        "1",
    ]

    try:
        stdout, stderr, returncode = await _run_in_container(container, cli_cmd, timeout, stdin_data=prompt)
    except Exception:
        logger.exception("Curation CLI invocation failed for %s", agent_name)
        return False

    if returncode != 0:
        logger.error("Curation CLI exited with code %d for %s: %s", returncode, agent_name, stderr[:200])
        return False

    # Parse result
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        logger.error("Could not parse curation result for %s", agent_name)
        return False

    if not isinstance(data, dict):
        logger.error("Curation result for %s was not a JSON object: %r", agent_name, data)
        return False

    new_memory = data.get("result", "")
    if not isinstance(new_memory, str):
        logger.error("Curation result for %s had a non-string 'result': %r", agent_name, new_memory)
        return False

    if not new_memory.strip():
        logger.warning("Curation returned empty result for %s", agent_name)
        return False

    # Safety check: don't write something wildly too large
    if len(new_memory.encode("utf-8")) > WORKING_MEMORY_MAX_BYTES * 2:
        logger.warning(
            "Curation result too large for %s: %d bytes (limit %d)",
            agent_name,
            len(new_memory.encode("utf-8")),
            WORKING_MEMORY_MAX_BYTES * 2,
        )
        return False

    # Acquire the per-agent lock before writing so that any append made to
    # memory.md during the long CLI await (above) is not silently overwritten.
    # Re-read the file under the lock and preserve any bytes that were added
    # after the snapshot we captured before the await.
    async with get_agent_lock(agent_name):
        fresh = _read_file(memory_path / "memory.md")
        delta = fresh[len(current_memory) :]
        final_memory = new_memory.strip()
        if delta.strip():
            final_memory += "\n" + delta
        write_memory(memory_path / "memory.md", final_memory)
    _write_marker(marker_path, today)
    logger.info("Curated memory for %s: %d bytes", agent_name, len(final_memory.encode("utf-8")))
    _rebuild_and_probe_index(agent_name, memory_path, len(final_memory.encode("utf-8")))
    return True


def _rebuild_and_probe_index(agent_name: str, memory_path: Path, memory_md_bytes: int) -> None:
    """Regenerate the structured-memory index and run the smoke probe (#640).

    Best-effort: index problems are logged, never fail the curation that
    already succeeded. Probes: (a) memory.md within cap, (b) manifest parses
    and references only existing files, (c) each known entity resolves to a
    single canonical file.
    """
    if memory_md_bytes > WORKING_MEMORY_MAX_BYTES:
        logger.warning(
            "Smoke probe: curated memory.md for %s is %d bytes (cap %d)",
            agent_name,
            memory_md_bytes,
            WORKING_MEMORY_MAX_BYTES,
        )
    try:
        alias_map = load_alias_map()
        build_index(memory_path, alias_map)
        for problem in verify_index(memory_path, alias_map):
            logger.warning("Smoke probe: memory index problem for %s: %s", agent_name, problem)
    except Exception:
        logger.exception("Failed to rebuild memory index for %s; continuing", agent_name)


def _get_last_curated_date(marker_path: Path) -> datetime.date | None:
    """Read the last curation date from the marker file."""
    if not marker_path.exists():
        return None
    try:
        date_str = marker_path.read_text(encoding="utf-8").strip()
        return datetime.date.fromisoformat(date_str)
    except (OSError, ValueError):
        return None


def _write_marker(marker_path: Path, date: datetime.date) -> None:
    """Write the curation date to the marker file."""
    _ensure_memory_dir(marker_path.parent)
    marker_path.write_text(date.isoformat(), encoding="utf-8")
    try:
        marker_path.chmod(MEMORY_FILE_MODE)
    except OSError:
        pass


def _read_file(path: Path) -> str:
    """Read a single file, return empty string if missing."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _read_new_dated_files(directory: Path, since_date: datetime.date | None) -> str:
    """Read .md files whose date-based filenames are after since_date."""
    if not directory.is_dir():
        return ""
    parts = []
    for f in sorted(directory.glob("*.md")):
        try:
            file_date = datetime.date.fromisoformat(f.stem)
            if since_date is None or file_date >= since_date:
                parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8')}")
        except ValueError:
            continue
    return "\n".join(parts)


def _read_modified_files(directory: Path, since_date: datetime.date | None) -> str:
    """Read .md files modified after the since_date."""
    if not directory.is_dir():
        return ""
    if since_date is None:
        # Read all files if no marker
        cutoff_ts = 0.0
    else:
        # Use the START of since_date as the cutoff so files modified later on
        # the same day curation last ran are still picked up (#459). Re-reading
        # the boundary day is harmless — the curation prompt merges/dedupes.
        cutoff_ts = datetime.datetime.combine(since_date, datetime.time.min).timestamp()

    parts = []
    for f in sorted(directory.glob("*.md")):
        try:
            if f.stat().st_mtime >= cutoff_ts:
                parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return "\n".join(parts)


def _collect_new_entries(memory_path: Path, since_date: datetime.date | None) -> str:
    """Collect all new long-term memory entries since the last curation."""
    sections = []

    daily = _read_new_dated_files(memory_path / "daily", since_date)
    if daily:
        sections.append(f"## Daily Logs\n{daily}")

    decisions = _read_new_dated_files(memory_path / "decisions", since_date)
    if decisions:
        sections.append(f"## Decisions\n{decisions}")

    people = _read_modified_files(memory_path / "people", since_date)
    if people:
        sections.append(f"## People\n{people}")

    projects = _read_modified_files(memory_path / "projects", since_date)
    if projects:
        sections.append(f"## Projects\n{projects}")

    systems = _read_modified_files(memory_path / "systems", since_date)
    if systems:
        sections.append(f"## Systems\n{systems}")

    prefs = _read_file(memory_path / "preferences" / "preferences.md")
    if prefs:
        sections.append(f"## Preferences\n{prefs}")

    return "\n\n".join(sections)


def _collect_trend_context(memory_path: Path, start: datetime.date, end: datetime.date) -> str:
    """Collect recent entries for trend awareness (read-only, not re-curated)."""
    sections = []

    daily_dir = memory_path / "daily"
    if daily_dir.is_dir():
        parts = []
        for f in sorted(daily_dir.glob("*.md")):
            try:
                file_date = datetime.date.fromisoformat(f.stem)
                if start <= file_date <= end:
                    parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8')}")
            except ValueError:
                continue
        if parts:
            sections.append("## Recent Daily Logs\n" + "\n".join(parts))

    decisions_dir = memory_path / "decisions"
    if decisions_dir.is_dir():
        parts = []
        for f in sorted(decisions_dir.glob("*.md")):
            try:
                file_date = datetime.date.fromisoformat(f.stem)
                if start <= file_date <= end:
                    parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8')}")
            except ValueError:
                continue
        if parts:
            sections.append("## Recent Decisions\n" + "\n".join(parts))

    return "\n\n".join(sections)
