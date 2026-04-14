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

    def test_persist_decisions(self, tmp_path):
        """Should persist decision entries to dated files."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {
            "decisions": [
                {"date": "2024-01-20", "topic": "Auth approach", "content": "Use OAuth2"},
            ],
        }
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        decision_file = memory_base / "decisions" / "2024-01-20.md"
        assert decision_file.exists()
        content = decision_file.read_text()
        assert "Auth approach" in content
        assert "OAuth2" in content

    def test_persist_preferences(self, tmp_path):
        """Should persist preference entries."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {
            "preferences": [
                {"date": "2024-01-20", "content": "Prefers short summaries"},
            ],
        }
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        pref_file = memory_base / "preferences" / "preferences.md"
        assert pref_file.exists()
        assert "short summaries" in pref_file.read_text()

    def test_persist_people(self, tmp_path):
        """Should persist people entries to name-based files."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {
            "people": [
                {"name": "John Doe", "context": "Backend engineer"},
            ],
        }
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        person_file = memory_base / "people" / "john-doe.md"
        assert person_file.exists()
        assert "Backend engineer" in person_file.read_text()

    def test_persist_projects(self, tmp_path):
        """Should persist project updates to name-based files."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {
            "projects": [
                {"name": "Auth Module", "update": "Added rate limiting"},
            ],
        }
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        project_file = memory_base / "projects" / "auth-module.md"
        assert project_file.exists()
        assert "rate limiting" in project_file.read_text()

    def test_persist_agent_memory(self, tmp_path):
        """Should append to agent's memory.md."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {"agent_memory": "Learned about the auth system."}
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        agent_memory_file = agent_base / "memory.md"
        assert agent_memory_file.exists()
        assert "auth system" in agent_memory_file.read_text()

    def test_persist_daily_log(self, tmp_path):
        """Should append to daily log file."""
        import datetime

        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {"daily_log": "Reviewed 3 PRs today."}
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        today = datetime.date.today().isoformat()
        log_file = memory_base / "daily" / f"{today}.md"
        assert log_file.exists()
        assert "3 PRs" in log_file.read_text()

    def test_persist_empty_updates(self, tmp_path):
        """Empty updates dict should persist nothing."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        count = memory_writer.persist_memory("lisa", {}, str(memory_base), str(agent_base))
        assert count == 0

    def test_persist_multiple_categories(self, tmp_path):
        """Should handle multiple categories in one call."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {
            "decisions": [{"date": "2024-01-20", "topic": "DB", "content": "Use Postgres"}],
            "agent_memory": "Decided on Postgres.",
            "daily_log": "DB decision made.",
        }
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 3

    def test_persist_uses_today_as_default_date(self, tmp_path):
        """Decisions without a date should use today's date."""
        import datetime

        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"

        updates = {"decisions": [{"topic": "Test", "content": "Something"}]}
        count = memory_writer.persist_memory("lisa", updates, str(memory_base), str(agent_base))
        assert count == 1
        today = datetime.date.today().isoformat()
        assert (memory_base / "decisions" / f"{today}.md").exists()

    def test_persist_empty_agent_memory_skipped(self, tmp_path):
        """Empty agent_memory string should not count as persisted."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        count = memory_writer.persist_memory("lisa", {"agent_memory": ""}, str(memory_base), str(agent_base))
        assert count == 0

    def test_persist_empty_daily_log_skipped(self, tmp_path):
        """Empty daily_log string should not count as persisted."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        count = memory_writer.persist_memory("lisa", {"daily_log": ""}, str(memory_base), str(agent_base))
        assert count == 0
