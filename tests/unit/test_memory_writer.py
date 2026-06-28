"""Unit tests for router.memory_writer — atomic writes, file creation, and append logic.

These tests define the interface that router/memory_writer.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

memory_writer = pytest.importorskip("router.memory_writer", reason="router.memory_writer not yet implemented")

pytestmark = pytest.mark.unit


class TestAtomicWrite:
    """Tests for atomic file writing."""

    def test_write_memory_creates_file(self, tmp_path):
        """write_memory() should create a new file with the given content."""
        target = tmp_path / "new_memory.md"
        memory_writer.write_memory(target, "# New Memory\nSome content.")
        assert target.exists()
        assert target.read_text() == "# New Memory\nSome content."

    def test_write_memory_overwrites_existing(self, tmp_path):
        """write_memory() should overwrite existing file content."""
        target = tmp_path / "existing.md"
        target.write_text("Old content")
        memory_writer.write_memory(target, "New content")
        assert target.read_text() == "New content"

    def test_write_memory_is_atomic(self, tmp_path):
        """Write should be atomic — file should not be partially written on failure.

        This test verifies the interface accepts the path and content.
        Actual atomicity testing requires integration-level testing.
        """
        target = tmp_path / "atomic_test.md"
        memory_writer.write_memory(target, "Complete content")
        assert target.read_text() == "Complete content"


class TestAppendLogic:
    """Tests for appending to memory files."""

    def test_append_memory_adds_content(self, tmp_path):
        """append_memory() should add content to the end of an existing file."""
        target = tmp_path / "append_test.md"
        target.write_text("# Memory\nExisting content.\n")
        memory_writer.append_memory(target, "\n## New Section\nAppended content.")
        content = target.read_text()
        assert "Existing content" in content
        assert "Appended content" in content

    def test_append_to_nonexistent_creates_file(self, tmp_path):
        """append_memory() on a nonexistent file should create it."""
        target = tmp_path / "new_append.md"
        memory_writer.append_memory(target, "# Fresh Content")
        assert target.exists()
        assert "Fresh Content" in target.read_text()


class TestDirectoryCreation:
    """Tests for automatic directory creation."""

    def test_write_creates_parent_directories(self, tmp_path):
        """write_memory() should create parent directories if they don't exist."""
        target = tmp_path / "agents" / "lisa" / "memory.md"
        memory_writer.write_memory(target, "# Lisa Memory")
        assert target.exists()
        assert target.read_text() == "# Lisa Memory"

    def test_append_creates_parent_directories(self, tmp_path):
        """append_memory() should create parent directories if they don't exist."""
        target = tmp_path / "agents" / "new_agent" / "memory.md"
        memory_writer.append_memory(target, "# New Agent Memory")
        assert target.exists()


class TestPermissions:
    """Tests for the 0600/0700 modes enforced for issue #116."""

    def test_write_memory_file_mode_is_0600(self, tmp_path):
        """Written memory files must be 0600 — other host users mustn't read them."""
        import stat

        target = tmp_path / "memory.md"
        memory_writer.write_memory(target, "secret notes")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_write_memory_parent_dir_mode_is_0700(self, tmp_path):
        """Auto-created parent dirs must be 0700 so the tree isn't browseable."""
        import stat

        target = tmp_path / "agents" / "lisa" / "memory" / "memory.md"
        memory_writer.write_memory(target, "content")
        mode = stat.S_IMODE(target.parent.stat().st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"

    def test_append_memory_file_mode_is_0600(self, tmp_path):
        """Files created via append_memory must also be 0600."""
        import stat

        target = tmp_path / "memory.md"
        memory_writer.append_memory(target, "content")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600

    @pytest.mark.asyncio
    async def test_persist_memory_files_are_0600(self, tmp_path):
        """Every file path persist_memory produces must end up 0600."""
        import datetime
        import stat

        agent_base = tmp_path / "agents"
        updates = {
            "decisions": [{"date": "2026-04-14", "topic": "T", "content": "C"}],
            "preferences": [{"date": "2026-04-14", "content": "P"}],
            "people": [{"name": "Bram", "context": "founder"}],
            "projects": [{"name": "Auth", "update": "shipped"}],
            "agent_memory": "note",
            "daily_log": "log",
        }
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        today = datetime.date.today().isoformat()
        files = [
            agent_base / "lisa" / "memory" / "decisions" / "2026-04-14.md",
            agent_base / "lisa" / "memory" / "preferences" / "preferences.md",
            agent_base / "lisa" / "memory" / "people" / "bram.md",
            agent_base / "lisa" / "memory" / "projects" / "auth.md",
            agent_base / "lisa" / "memory" / "memory.md",
            agent_base / "lisa" / "memory" / "daily" / f"{today}.md",
        ]
        for f in files:
            assert f.exists(), f"{f} missing"
            mode = stat.S_IMODE(f.stat().st_mode)
            assert mode == 0o600, f"{f} mode {oct(mode)} != 0o600"


class TestWriteMemoryErrorHandling:
    """Tests for error handling during atomic writes."""

    def test_write_memory_cleans_up_on_error(self, tmp_path, monkeypatch):
        """If os.rename fails, temp file should be cleaned up."""
        import os

        target = tmp_path / "fail_test.md"

        def failing_rename(src, dst):
            raise OSError("rename failed")

        monkeypatch.setattr(os, "rename", failing_rename)

        with pytest.raises(OSError, match="rename failed"):
            memory_writer.write_memory(target, "content")

        # Temp files should be cleaned up
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestPersistMemory:
    """Tests for the persist_memory function."""

    @pytest.mark.asyncio
    async def test_persist_decisions(self, tmp_path):
        """Should persist decision entries to dated files."""
        agent_base = tmp_path / "agents"

        updates = {
            "decisions": [
                {"date": "2024-01-20", "topic": "Auth approach", "content": "Use OAuth2"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        decision_file = agent_base / "lisa" / "memory" / "decisions" / "2024-01-20.md"
        assert decision_file.exists()
        content = decision_file.read_text()
        assert "Auth approach" in content
        assert "OAuth2" in content

    @pytest.mark.asyncio
    async def test_persist_preferences(self, tmp_path):
        """Should persist preference entries."""
        agent_base = tmp_path / "agents"

        updates = {
            "preferences": [
                {"date": "2024-01-20", "content": "Prefers short summaries"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        pref_file = agent_base / "lisa" / "memory" / "preferences" / "preferences.md"
        assert pref_file.exists()
        assert "short summaries" in pref_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_people(self, tmp_path):
        """Should persist people entries to name-based files."""
        agent_base = tmp_path / "agents"

        updates = {
            "people": [
                {"name": "John Doe", "context": "Backend engineer"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        person_file = agent_base / "lisa" / "memory" / "people" / "john-doe.md"
        assert person_file.exists()
        assert "Backend engineer" in person_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_projects(self, tmp_path):
        """Should persist project updates to name-based files."""
        agent_base = tmp_path / "agents"

        updates = {
            "projects": [
                {"name": "Auth Module", "update": "Added rate limiting"},
            ],
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        project_file = agent_base / "lisa" / "memory" / "projects" / "auth-module.md"
        assert project_file.exists()
        assert "rate limiting" in project_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_agent_memory(self, tmp_path):
        """Should append to agent's memory.md."""
        agent_base = tmp_path / "agents"

        updates = {"agent_memory": "Learned about the auth system."}
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        agent_memory_file = agent_base / "lisa" / "memory" / "memory.md"
        assert agent_memory_file.exists()
        assert "auth system" in agent_memory_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_daily_log(self, tmp_path):
        """Should append to daily log file."""
        import datetime

        agent_base = tmp_path / "agents"

        updates = {"daily_log": "Reviewed 3 PRs today."}
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        today = datetime.date.today().isoformat()
        log_file = agent_base / "lisa" / "memory" / "daily" / f"{today}.md"
        assert log_file.exists()
        assert "3 PRs" in log_file.read_text()

    @pytest.mark.asyncio
    async def test_persist_empty_updates(self, tmp_path):
        """Empty updates dict should persist nothing."""
        agent_base = tmp_path / "agents"
        count = await memory_writer.persist_memory("lisa", {}, str(agent_base))
        assert count == 0

    @pytest.mark.asyncio
    async def test_persist_multiple_categories(self, tmp_path):
        """Should handle multiple categories in one call."""
        agent_base = tmp_path / "agents"

        updates = {
            "decisions": [{"date": "2024-01-20", "topic": "DB", "content": "Use Postgres"}],
            "agent_memory": "Decided on Postgres.",
            "daily_log": "DB decision made.",
        }
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 3

    @pytest.mark.asyncio
    async def test_persist_uses_today_as_default_date(self, tmp_path):
        """Decisions without a date should use today's date."""
        import datetime

        agent_base = tmp_path / "agents"

        updates = {"decisions": [{"topic": "Test", "content": "Something"}]}
        count = await memory_writer.persist_memory("lisa", updates, str(agent_base))
        assert count == 1
        today = datetime.date.today().isoformat()
        assert (agent_base / "lisa" / "memory" / "decisions" / f"{today}.md").exists()

    @pytest.mark.asyncio
    async def test_persist_empty_agent_memory_skipped(self, tmp_path):
        """Empty agent_memory string should not count as persisted."""
        agent_base = tmp_path / "agents"
        count = await memory_writer.persist_memory("lisa", {"agent_memory": ""}, str(agent_base))
        assert count == 0

    @pytest.mark.asyncio
    async def test_persist_empty_daily_log_skipped(self, tmp_path):
        """Empty daily_log string should not count as persisted."""
        agent_base = tmp_path / "agents"
        count = await memory_writer.persist_memory("lisa", {"daily_log": ""}, str(agent_base))
        assert count == 0


class TestDecisionDateNormalization:
    """Regression tests for issue #488 — non-ISO decision dates must still produce ISO filenames."""

    def _decisions_dir(self, agent_base, agent="lisa"):
        return agent_base / agent / "memory" / "decisions"

    def _iso_stem(self, path):
        """Assert and return the ISO-parsed stem, raising AssertionError if invalid."""
        import datetime

        try:
            return datetime.date.fromisoformat(path.stem)
        except ValueError:
            raise AssertionError(f"File stem {path.stem!r} is not a valid ISO date") from None

    @pytest.mark.asyncio
    async def test_long_form_date_normalised(self, tmp_path):
        """'June 19, 2026' must produce 2026-06-19.md, readable by the curator."""
        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "June 19, 2026", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        self._iso_stem(files[0])
        assert files[0].stem == "2026-06-19"

    @pytest.mark.asyncio
    async def test_slash_separated_date_normalised(self, tmp_path):
        """'2026/06/19' must produce 2026-06-19.md and not escape decisions/ via path sep."""
        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "2026/06/19", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        decisions_dir = self._decisions_dir(agent_base)
        files = list(decisions_dir.glob("*.md"))
        assert len(files) == 1, f"Expected 1 file in decisions/, got: {[f.name for f in files]}"
        self._iso_stem(files[0])
        assert files[0].stem == "2026-06-19"
        # File must sit directly inside decisions/, not a subdirectory
        assert files[0].parent == decisions_dir

    @pytest.mark.asyncio
    async def test_datetime_string_normalised(self, tmp_path):
        """A date+time string like '2026-06-19T14:30:00' must produce 2026-06-19.md."""
        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "2026-06-19T14:30:00", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        self._iso_stem(files[0])
        assert files[0].stem == "2026-06-19"

    @pytest.mark.asyncio
    async def test_empty_date_falls_back_to_today(self, tmp_path):
        """An empty date string must fall back to today and produce a valid ISO filename."""
        import datetime

        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        parsed = self._iso_stem(files[0])
        assert parsed == datetime.date.today()

    @pytest.mark.asyncio
    async def test_unparseable_date_falls_back_to_today(self, tmp_path):
        """A date string that cannot be parsed at all must fall back to today."""
        import datetime

        agent_base = tmp_path / "agents"
        updates = {"decisions": [{"date": "not-a-date", "topic": "T", "content": "C"}]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        files = list(self._decisions_dir(agent_base).glob("*.md"))
        assert len(files) == 1
        parsed = self._iso_stem(files[0])
        assert parsed == datetime.date.today()

    @pytest.mark.asyncio
    async def test_normalised_file_is_curator_visible(self, tmp_path):
        """Every written decision file stem must parse via datetime.date.fromisoformat."""
        import datetime

        agent_base = tmp_path / "agents"
        bad_dates = ["June 19, 2026", "2026/06/19", "2026-06-19T12:00:00", "", "??"]
        updates = {"decisions": [{"date": d, "topic": "T", "content": "C"} for d in bad_dates]}
        await memory_writer.persist_memory("lisa", updates, str(agent_base))

        for f in self._decisions_dir(agent_base).glob("*.md"):
            # This is the exact check in memory_curator._read_new_dated_files; it must not raise.
            datetime.date.fromisoformat(f.stem)
