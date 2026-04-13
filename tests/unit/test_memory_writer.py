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
