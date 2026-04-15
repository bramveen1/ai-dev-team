"""Unit tests for router.memory_curator — incremental memory curation.

Tests verify:
- needs_curation() detects when curation is needed
- Date-based file filtering reads only new entries
- Modification-time file filtering works correctly
- curate_agent_memory() invokes CLI and writes results
- .last_curated marker is updated after successful curation
"""

import datetime
import json
from unittest.mock import AsyncMock, patch

import pytest

from router.memory_curator import (
    MARKER_FILENAME,
    _collect_new_entries,
    _read_file,
    _read_modified_files,
    _read_new_dated_files,
    curate_agent_memory,
    needs_curation,
)

pytestmark = pytest.mark.unit


class TestNeedsCuration:
    """Tests for the needs_curation check."""

    def test_needs_curation_no_marker(self, tmp_path):
        """Should return True when .last_curated doesn't exist."""
        agent_base = tmp_path / "agents"
        (agent_base / "lisa" / "memory").mkdir(parents=True)
        assert needs_curation("lisa", str(agent_base)) is True

    def test_needs_curation_stale_marker(self, tmp_path):
        """Should return True when marker is from a previous day."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        memory_dir.mkdir(parents=True)
        marker = memory_dir / MARKER_FILENAME
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        marker.write_text(yesterday)
        assert needs_curation("lisa", str(agent_base)) is True

    def test_needs_curation_fresh_marker(self, tmp_path):
        """Should return False when marker is from today."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        memory_dir.mkdir(parents=True)
        marker = memory_dir / MARKER_FILENAME
        marker.write_text(datetime.date.today().isoformat())
        assert needs_curation("lisa", str(agent_base)) is False

    def test_needs_curation_corrupt_marker(self, tmp_path):
        """Should return True when marker contains invalid data."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        memory_dir.mkdir(parents=True)
        marker = memory_dir / MARKER_FILENAME
        marker.write_text("not-a-date")
        assert needs_curation("lisa", str(agent_base)) is True


class TestReadNewDatedFiles:
    """Tests for _read_new_dated_files filtering."""

    def test_reads_files_after_date(self, tmp_path):
        """Should only read files with dates after since_date."""
        daily = tmp_path / "daily"
        daily.mkdir()
        (daily / "2026-04-10.md").write_text("old entry")
        (daily / "2026-04-14.md").write_text("new entry")
        (daily / "2026-04-15.md").write_text("newest entry")

        since = datetime.date(2026, 4, 13)
        result = _read_new_dated_files(daily, since)
        assert "new entry" in result
        assert "newest entry" in result
        assert "old entry" not in result

    def test_reads_all_when_no_marker(self, tmp_path):
        """Should read all files when since_date is None."""
        daily = tmp_path / "daily"
        daily.mkdir()
        (daily / "2026-04-10.md").write_text("entry one")
        (daily / "2026-04-14.md").write_text("entry two")

        result = _read_new_dated_files(daily, None)
        assert "entry one" in result
        assert "entry two" in result

    def test_empty_directory(self, tmp_path):
        """Should return empty string for empty directory."""
        daily = tmp_path / "daily"
        daily.mkdir()
        result = _read_new_dated_files(daily, None)
        assert result == ""

    def test_nonexistent_directory(self, tmp_path):
        """Should return empty string for missing directory."""
        result = _read_new_dated_files(tmp_path / "nonexistent", None)
        assert result == ""

    def test_ignores_non_date_filenames(self, tmp_path):
        """Should skip files that don't have date-format names."""
        daily = tmp_path / "daily"
        daily.mkdir()
        (daily / "notes.md").write_text("not a dated file")
        (daily / "2026-04-14.md").write_text("dated file")

        result = _read_new_dated_files(daily, None)
        assert "dated file" in result
        assert "not a dated file" not in result


class TestReadModifiedFiles:
    """Tests for _read_modified_files filtering."""

    def test_reads_all_when_no_marker(self, tmp_path):
        """Should read all files when since_date is None."""
        people = tmp_path / "people"
        people.mkdir()
        (people / "bram.md").write_text("Bram info")
        result = _read_modified_files(people, None)
        assert "Bram info" in result

    def test_nonexistent_directory(self, tmp_path):
        """Should return empty string for missing directory."""
        result = _read_modified_files(tmp_path / "nonexistent", None)
        assert result == ""


class TestReadFile:
    """Tests for _read_file helper."""

    def test_reads_existing_file(self, tmp_path):
        """Should return file content."""
        f = tmp_path / "test.md"
        f.write_text("content")
        assert _read_file(f) == "content"

    def test_missing_file_returns_empty(self, tmp_path):
        """Should return empty string for missing file."""
        assert _read_file(tmp_path / "missing.md") == ""


class TestCollectNewEntries:
    """Tests for _collect_new_entries aggregation."""

    def test_collects_from_multiple_categories(self, tmp_path):
        """Should aggregate entries from daily, decisions, people, projects."""
        memory = tmp_path / "memory"
        (memory / "daily").mkdir(parents=True)
        (memory / "decisions").mkdir()
        (memory / "people").mkdir()

        (memory / "daily" / "2026-04-14.md").write_text("did stuff")
        (memory / "decisions" / "2026-04-14.md").write_text("decided things")
        (memory / "people" / "bram.md").write_text("Bram context")

        result = _collect_new_entries(memory, None)
        assert "did stuff" in result
        assert "decided things" in result
        assert "Bram context" in result

    def test_empty_memory_dir(self, tmp_path):
        """Should return empty string when no memory subdirs exist."""
        memory = tmp_path / "memory"
        memory.mkdir()
        result = _collect_new_entries(memory, None)
        assert result == ""


class TestCurateAgentMemory:
    """Tests for the main curation function."""

    @pytest.mark.asyncio
    async def test_curate_writes_memory_and_marker(self, tmp_path):
        """Should write curated memory.md and update .last_curated."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("Had a productive day")

        curated_content = "## Key People\n- Bram: founder\n\n## Notes\nProductive day"
        mock_stdout = json.dumps({"result": curated_content})

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (mock_stdout, "", 0)
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is True
        assert (memory_dir / "memory.md").exists()
        assert (memory_dir / MARKER_FILENAME).exists()
        assert curated_content in (memory_dir / "memory.md").read_text()
        assert datetime.date.today().isoformat() in (memory_dir / MARKER_FILENAME).read_text()

    @pytest.mark.asyncio
    async def test_curate_skips_when_nothing_new(self, tmp_path):
        """Should skip CLI invocation when no new entries and memory exists."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "memory.md").write_text("existing memory")

        result = await curate_agent_memory("lisa", "lisa", str(agent_base))
        assert result is True
        assert (memory_dir / MARKER_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_curate_handles_cli_failure(self, tmp_path):
        """Should return False when CLI fails."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("", "error", 1)
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is False
        assert not (memory_dir / MARKER_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_curate_rejects_oversized_result(self, tmp_path):
        """Should reject curation results that are way too large."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        huge_content = "x" * 10000
        mock_stdout = json.dumps({"result": huge_content})

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (mock_stdout, "", 0)
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is False
