"""Unit tests for router.memory_curator — incremental memory curation.

Tests verify:
- needs_curation() detects when curation is needed
- Date-based file filtering reads only new entries
- Modification-time file filtering works correctly
- curate_agent_memory() invokes CLI and writes results
- .last_curated marker is updated after successful curation
"""

import asyncio
import datetime
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

import router.memory_curator as _curator_mod
from router.memory_curator import (
    MARKER_FILENAME,
    _collect_new_entries,
    _read_file,
    _read_modified_files,
    _read_new_dated_files,
    curate_agent_memory,
    is_curation_in_flight,
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

    def test_reads_file_on_since_date_boundary(self, tmp_path):
        """Should include files whose date equals since_date (>= not >).

        Regression: same-day post-curation entries were silently dropped
        because the old condition used strict greater-than, so a file with
        file_date == since_date was never picked up on the following run.
        """
        daily = tmp_path / "daily"
        daily.mkdir()
        since = datetime.date(2026, 4, 14)
        (daily / "2026-04-14.md").write_text("boundary day entry")
        (daily / "2026-04-13.md").write_text("before boundary")

        result = _read_new_dated_files(daily, since)
        assert "boundary day entry" in result
        assert "before boundary" not in result


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

    def test_reads_file_modified_on_boundary_day(self, tmp_path):
        """People/projects files modified later on the last-curated day must be
        picked up (#459).

        Curation ran on day D (marker = D), then a people/*.md file was edited
        at 15:00 on day D. On day D+1 the run uses since_date == D. The old
        cutoff (end-of-day D) excluded that file forever; the start-of-day
        cutoff includes it.
        """
        people = tmp_path / "people"
        people.mkdir()
        since = datetime.date(2026, 4, 13)

        boundary = people / "bram.md"
        boundary.write_text("edited on boundary day")
        # mtime = 15:00 on the since_date (D) — after curation last ran that day.
        mtime_15h = datetime.datetime.combine(since, datetime.time(15, 0)).timestamp()
        os.utime(boundary, (mtime_15h, mtime_15h))

        before = people / "old.md"
        before.write_text("edited the previous day")
        mtime_prev = datetime.datetime.combine(since - datetime.timedelta(days=1), datetime.time(15, 0)).timestamp()
        os.utime(before, (mtime_prev, mtime_prev))

        result = _read_modified_files(people, since)
        assert "edited on boundary day" in result
        assert "edited the previous day" not in result


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

    def test_preferences_included_on_non_first_curation(self, tmp_path):
        """Preferences must appear in curation even when since_date is set.

        Regression: the old guard `if prefs and since_date is None` silently
        dropped every preference recorded after the first curation run.
        """
        memory = tmp_path / "memory"
        (memory / "preferences").mkdir(parents=True)
        (memory / "preferences" / "preferences.md").write_text("prefer dark mode")

        since = datetime.date(2026, 4, 13)
        result = _collect_new_entries(memory, since)
        assert "prefer dark mode" in result

    def test_preferences_included_on_first_curation(self, tmp_path):
        """Preferences must also appear on the very first run (since_date=None)."""
        memory = tmp_path / "memory"
        (memory / "preferences").mkdir(parents=True)
        (memory / "preferences" / "preferences.md").write_text("prefer concise replies")

        result = _collect_new_entries(memory, None)
        assert "prefer concise replies" in result

    def test_preferences_absent_when_file_empty(self, tmp_path):
        """Empty preferences.md should not produce a Preferences section."""
        memory = tmp_path / "memory"
        (memory / "preferences").mkdir(parents=True)
        (memory / "preferences" / "preferences.md").write_text("")

        result = _collect_new_entries(memory, datetime.date(2026, 4, 13))
        assert "Preferences" not in result


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
    async def test_curate_writes_marker_with_0600_mode(self, tmp_path):
        """The .last_curated marker must be 0600 — agents (uid 1000) must
        own and be the only readers/writers of their own memory files.

        Regression guard for issue #116, where the curator (running as
        root) was leaving root-owned 0644 files agents couldn't write."""
        import stat

        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "memory.md").write_text("existing")

        # Nothing-new path: still writes the marker, no CLI call needed.
        result = await curate_agent_memory("lisa", "lisa", str(agent_base))
        assert result is True

        marker = memory_dir / MARKER_FILENAME
        assert marker.exists()
        mode = stat.S_IMODE(marker.stat().st_mode)
        assert mode == 0o600, f"marker mode {oct(mode)} != 0o600"

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

    @pytest.mark.asyncio
    async def test_curate_preserves_appends_during_await(self, tmp_path):
        """Appends to memory.md that arrive during the curator's CLI await must not be lost.

        Simulates the lost-update race: the curator reads memory.md, then yields
        (await CLI), a concurrent session-end appends to memory.md, then the curator
        resumes. Without the re-read+merge fix the append is silently overwritten.
        """
        from router.memory_writer import append_memory, get_agent_lock

        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("Had a productive day")

        curated_content = "## Curated\n- Key insight from curation"
        session_note = "\nSession note appended during curation await\n"
        memory_md = memory_dir / "memory.md"

        async def fake_run_in_container(container, cmd, timeout):
            # Yield so any queued coroutines (the concurrent append) can run.
            await asyncio.sleep(0)
            return json.dumps({"result": curated_content}), "", 0

        async def concurrent_append():
            async with get_agent_lock("lisa"):
                append_memory(memory_md, session_note)

        # Schedule the concurrent append before starting curation so it can
        # run when the curator yields inside _run_in_container.
        append_task = asyncio.create_task(concurrent_append())

        with patch("router.memory_curator._run_in_container", side_effect=fake_run_in_container):
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        await append_task

        assert result is True
        content = memory_md.read_text()
        assert "Key insight from curation" in content, "curated content must be present"
        assert "Session note appended during curation await" in content, "concurrent append must be preserved"


class TestCurationInFlightGuard:
    """Regression tests for issue #511 — in-flight guard prevents concurrent curations."""

    @pytest.fixture(autouse=True)
    def _clear_guard(self):
        """Ensure the module-level guard is empty before and after each test."""
        _curator_mod._curation_in_flight.clear()
        yield
        _curator_mod._curation_in_flight.clear()

    def test_is_curation_in_flight_false_initially(self):
        """Guard should report no curation in flight at startup."""
        assert is_curation_in_flight("lisa") is False

    @pytest.mark.asyncio
    async def test_burst_of_messages_invokes_run_in_container_once(self, tmp_path):
        """Simulates N concurrent curate_agent_memory calls (burst of messages).

        _run_in_container must be called exactly once even when N tasks start
        concurrently while the first is still blocking in the container call.
        """
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        curated_content = "## Notes\nsome memory"
        mock_stdout = json.dumps({"result": curated_content})

        # Use an asyncio.Event so the first call blocks until we're ready to
        # release it, ensuring all N tasks are started before any finishes.
        gate = asyncio.Event()

        async def slow_container(container, cmd, timeout):
            await gate.wait()
            return (mock_stdout, "", 0)

        N = 5
        with patch("router.memory_curator._run_in_container", side_effect=slow_container) as mock_run:
            tasks = [asyncio.create_task(curate_agent_memory("lisa", "lisa", str(agent_base))) for _ in range(N)]
            # Give all tasks a chance to start and hit the guard check.
            await asyncio.sleep(0)
            # Release the one task that acquired the guard.
            gate.set()
            results = await asyncio.gather(*tasks)

        # Exactly one call should have reached _run_in_container.
        assert mock_run.call_count == 1
        # Only one task should have returned True; the rest (skipped) return False.
        assert results.count(True) == 1
        assert results.count(False) == N - 1

    @pytest.mark.asyncio
    async def test_guard_cleared_after_success(self, tmp_path):
        """Guard must be cleared after a successful curation so future messages work."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        curated_content = "## Notes\nsome memory"
        mock_stdout = json.dumps({"result": curated_content})

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (mock_stdout, "", 0)
            await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert is_curation_in_flight("lisa") is False

    @pytest.mark.asyncio
    async def test_guard_cleared_after_failure(self, tmp_path):
        """Guard must be cleared after a failed curation so a retry is possible."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("", "error", 1)
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is False
        assert is_curation_in_flight("lisa") is False

    @pytest.mark.asyncio
    async def test_guard_cleared_after_exception(self, tmp_path):
        """Guard must be cleared even when _run_in_container raises."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("container gone")
            result = await curate_agent_memory("lisa", "lisa", str(agent_base))

        assert result is False
        assert is_curation_in_flight("lisa") is False

    @pytest.mark.asyncio
    async def test_new_day_triggers_fresh_curation_after_guard_clears(self, tmp_path):
        """After a successful curation (guard cleared), a new-day call works."""
        agent_base = tmp_path / "agents"
        memory_dir = agent_base / "lisa" / "memory"
        (memory_dir / "daily").mkdir(parents=True)
        (memory_dir / "daily" / "2026-04-14.md").write_text("entry")

        curated_content = "## Notes\nsome memory"
        mock_stdout = json.dumps({"result": curated_content})

        with patch("router.memory_curator._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (mock_stdout, "", 0)

            # First curation succeeds.
            result1 = await curate_agent_memory("lisa", "lisa", str(agent_base))
            assert result1 is True
            assert mock_run.call_count == 1

            # Simulate new day by backdating the marker.
            marker = memory_dir / MARKER_FILENAME
            marker.write_text("2000-01-01")

            # A subsequent call should run again (guard was cleared).
            result2 = await curate_agent_memory("lisa", "lisa", str(agent_base))
            assert result2 is True
            assert mock_run.call_count == 2
