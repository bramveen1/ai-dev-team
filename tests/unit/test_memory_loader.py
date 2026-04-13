"""Unit tests for router.memory_loader — file reading and size tracking.

These tests define the interface that router/memory_loader.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

memory_loader = pytest.importorskip("router.memory_loader", reason="router.memory_loader not yet implemented")

pytestmark = pytest.mark.unit


class TestFileLoading:
    """Tests for loading memory files from disk."""

    def test_load_memory_returns_string(self, test_memory_dir):
        """load_memory() should return file content as a string."""
        result = memory_loader.load_memory(test_memory_dir / "MEMORY.md")
        assert isinstance(result, str)

    def test_load_memory_contains_content(self, test_memory_dir):
        """Loaded memory should contain expected content from the fixture."""
        result = memory_loader.load_memory(test_memory_dir / "MEMORY.md")
        assert "Architecture Decisions" in result

    def test_load_agent_memory(self, test_memory_dir):
        """Should load agent-specific memory files."""
        result = memory_loader.load_memory(test_memory_dir / "agents" / "lisa" / "memory.md")
        assert "Lisa" in result


class TestMissingFileHandling:
    """Tests for handling missing or inaccessible memory files."""

    def test_missing_file_returns_empty_string(self, tmp_path):
        """Loading a nonexistent file should return an empty string (not raise)."""
        result = memory_loader.load_memory(tmp_path / "nonexistent.md")
        assert result == ""

    def test_missing_directory_returns_empty_string(self):
        """Loading from a nonexistent directory should return an empty string."""
        result = memory_loader.load_memory("/tmp/nonexistent_dir_12345/memory.md")
        assert result == ""


class TestSizeTracking:
    """Tests for tracking memory file sizes."""

    def test_get_memory_size_returns_int(self, test_memory_dir):
        """get_memory_size() should return the file size in bytes as an integer."""
        result = memory_loader.get_memory_size(test_memory_dir / "MEMORY.md")
        assert isinstance(result, int)
        assert result > 0

    def test_get_memory_size_missing_file(self):
        """get_memory_size() for a missing file should return 0."""
        result = memory_loader.get_memory_size("/tmp/nonexistent_12345.md")
        assert result == 0

    def test_load_all_memory_returns_dict(self, test_memory_dir):
        """load_all_memory() should return a dict mapping file paths to content."""
        result = memory_loader.load_all_memory(test_memory_dir)
        assert isinstance(result, dict)
        assert len(result) > 0
