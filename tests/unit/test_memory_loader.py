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


class TestLoadAllMemoryEdgeCases:
    """Tests for load_all_memory edge cases."""

    def test_nonexistent_directory_returns_empty_dict(self):
        """load_all_memory() with a nonexistent directory should return {}."""
        result = memory_loader.load_all_memory("/tmp/nonexistent_dir_99999")
        assert result == {}

    def test_empty_directory_returns_empty_dict(self, tmp_path):
        """load_all_memory() with empty directory should return {}."""
        result = memory_loader.load_all_memory(tmp_path)
        assert result == {}


class TestLoadAgentMemory:
    """Tests for load_agent_memory function."""

    def test_returns_expected_keys(self, tmp_path):
        """Should return dict with org_memory, agent_memory, system_docs."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        memory_base.mkdir()
        agent_base.mkdir()
        (memory_base / "MEMORY.md").write_text("# Org Memory")
        (agent_base / "memory.md").write_text("# Agent Memory")

        result = memory_loader.load_agent_memory(
            "lisa",
            memory_base=str(memory_base),
            agent_base=str(agent_base),
        )
        assert "org_memory" in result
        assert "agent_memory" in result
        assert "system_docs" in result
        assert "Org Memory" in result["org_memory"]
        assert "Agent Memory" in result["agent_memory"]

    def test_loads_system_docs(self, tmp_path):
        """Should load system docs when agent_tools mapping is provided."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        systems_base = tmp_path / "systems"
        memory_base.mkdir()
        agent_base.mkdir()
        systems_base.mkdir()
        (systems_base / "outlook.md").write_text("# Outlook API")

        result = memory_loader.load_agent_memory(
            "lisa",
            memory_base=str(memory_base),
            agent_base=str(agent_base),
            systems_base=str(systems_base),
            agent_tools={"lisa": ["outlook.md"]},
        )
        assert len(result["system_docs"]) == 1
        assert "Outlook API" in result["system_docs"][0]

    def test_skips_missing_system_docs(self, tmp_path):
        """Should skip system doc files that don't exist."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        systems_base = tmp_path / "systems"
        memory_base.mkdir()
        agent_base.mkdir()
        systems_base.mkdir()

        result = memory_loader.load_agent_memory(
            "lisa",
            memory_base=str(memory_base),
            agent_base=str(agent_base),
            systems_base=str(systems_base),
            agent_tools={"lisa": ["nonexistent.md"]},
        )
        assert result["system_docs"] == []

    def test_no_agent_tools_returns_empty_system_docs(self, tmp_path):
        """Without agent_tools, system_docs should be empty."""
        memory_base = tmp_path / "memory"
        agent_base = tmp_path / "agent"
        memory_base.mkdir()
        agent_base.mkdir()

        result = memory_loader.load_agent_memory(
            "lisa",
            memory_base=str(memory_base),
            agent_base=str(agent_base),
        )
        assert result["system_docs"] == []
