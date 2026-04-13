"""Integration tests for memory read/write roundtrips.

Tests that memory can be written and read back correctly using actual
filesystem operations in a temp directory.
Tests will SKIP until the required modules exist.
"""

import pytest

pytestmark = pytest.mark.integration


class TestMemoryRoundtrip:
    """Test writing and reading memory files end-to-end."""

    def test_write_then_load(self, tmp_path):
        """Content written with memory_writer should be readable by memory_loader."""
        try:
            from router.memory_writer import write_memory

            from router.memory_loader import load_memory
        except ImportError:
            pytest.skip("Router memory modules not yet implemented")

        target = tmp_path / "MEMORY.md"
        content = "# Team Memory\n\n## Decisions\n- Use pytest for testing\n"
        write_memory(target, content)
        loaded = load_memory(target)
        assert loaded == content

    def test_append_then_load(self, tmp_path):
        """Content appended with memory_writer should be readable by memory_loader."""
        try:
            from router.memory_writer import append_memory, write_memory

            from router.memory_loader import load_memory
        except ImportError:
            pytest.skip("Router memory modules not yet implemented")

        target = tmp_path / "agent_memory.md"
        write_memory(target, "# Lisa Memory\n")
        append_memory(target, "\n## Session 1\n- Fixed auth bug\n")
        loaded = load_memory(target)
        assert "Lisa Memory" in loaded
        assert "Fixed auth bug" in loaded

    def test_nested_directory_roundtrip(self, tmp_path):
        """Writing to nested paths should create directories and be loadable."""
        try:
            from router.memory_writer import write_memory

            from router.memory_loader import load_memory
        except ImportError:
            pytest.skip("Router memory modules not yet implemented")

        target = tmp_path / "agents" / "lisa" / "memory.md"
        write_memory(target, "# Lisa\nSession notes here.")
        loaded = load_memory(target)
        assert "Lisa" in loaded

    def test_load_all_from_written_directory(self, tmp_path):
        """load_all_memory() should find all files written to a directory tree."""
        try:
            from router.memory_writer import write_memory

            from router.memory_loader import load_all_memory
        except ImportError:
            pytest.skip("Router memory modules not yet implemented")

        write_memory(tmp_path / "MEMORY.md", "# Team")
        write_memory(tmp_path / "agents" / "lisa" / "memory.md", "# Lisa")
        all_memory = load_all_memory(tmp_path)
        assert len(all_memory) >= 2
