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

from router.dispatcher import _run_in_container
from router.memory_writer import WORKING_MEMORY_MAX_BYTES, write_memory

logger = logging.getLogger(__name__)

TREND_LOOKBACK_DAYS = 5
MARKER_FILENAME = ".last_curated"

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
- Sections: ## Key People, ## Active Projects, ## Recent Decisions, ## Preferences, ## Notes
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
        prompt,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--max-turns",
        "1",
    ]

    try:
        stdout, stderr, returncode = await _run_in_container(container, cli_cmd, timeout)
    except Exception:
        logger.exception("Curation CLI invocation failed for %s", agent_name)
        return False

    if returncode != 0:
        logger.error("Curation CLI exited with code %d for %s: %s", returncode, agent_name, stderr[:200])
        return False

    # Parse result
    try:
        data = json.loads(stdout)
        new_memory = data.get("result", "")
    except (json.JSONDecodeError, TypeError):
        logger.error("Could not parse curation result for %s", agent_name)
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

    write_memory(memory_path / "memory.md", new_memory)
    _write_marker(marker_path, today)
    logger.info("Curated memory for %s: %d bytes", agent_name, len(new_memory.encode("utf-8")))
    return True


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
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(date.isoformat(), encoding="utf-8")


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
            if since_date is None or file_date > since_date:
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
        cutoff_ts = datetime.datetime.combine(since_date, datetime.time.max).timestamp()

    parts = []
    for f in sorted(directory.glob("*.md")):
        try:
            if f.stat().st_mtime > cutoff_ts:
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

    prefs = _read_file(memory_path / "preferences" / "preferences.md")
    if prefs and since_date is None:
        # Include preferences on first curation
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
