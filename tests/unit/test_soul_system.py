"""Unit tests for the SOUL system — shared personality + per-agent role separation.

Tests verify:
- SOUL.md is loaded and included in context
- personality.md is loaded per-agent and included in context
- Loading order: SOUL -> role -> personality -> memory -> org memory
- No duplication between SOUL and personality content
- Missing files are handled gracefully
"""

import pytest

from router.context_builder import build_context
from router.memory_loader import load_agent_context, load_memory

pytestmark = pytest.mark.unit


class TestSoulInContext:
    """Tests that SOUL.md content is included in assembled context."""

    def test_context_includes_soul(self):
        """build_context() should include SOUL content when provided."""
        result = build_context(
            role_md="# Lisa\nProject manager.",
            memory="",
            thread_history=[],
            system_docs="",
            soul_md="# SOUL\nBe genuinely helpful.",
        )
        assert "genuinely helpful" in result

    def test_context_includes_personality(self):
        """build_context() should include personality content when provided."""
        result = build_context(
            role_md="# Lisa\nProject manager.",
            memory="",
            thread_history=[],
            system_docs="",
            personality_md="# Lisa Personality\nWarm and encouraging.",
        )
        assert "Warm and encouraging" in result

    def test_context_includes_both_soul_and_personality(self):
        """build_context() should include both SOUL and personality."""
        result = build_context(
            role_md="# Lisa\nProject manager.",
            memory="",
            thread_history=[],
            system_docs="",
            soul_md="# SOUL\nBe genuinely helpful.",
            personality_md="# Lisa Personality\nWarm and encouraging.",
        )
        assert "genuinely helpful" in result
        assert "Warm and encouraging" in result

    def test_soul_before_role(self):
        """SOUL should appear before role in assembled context."""
        result = build_context(
            role_md="ROLE_MARKER",
            memory="",
            thread_history=[],
            system_docs="",
            soul_md="SOUL_MARKER",
        )
        assert result.index("SOUL_MARKER") < result.index("ROLE_MARKER")

    def test_role_before_personality(self):
        """Role should appear before personality in assembled context."""
        result = build_context(
            role_md="ROLE_MARKER",
            memory="",
            thread_history=[],
            system_docs="",
            personality_md="PERSONALITY_MARKER",
        )
        assert result.index("ROLE_MARKER") < result.index("PERSONALITY_MARKER")

    def test_personality_before_memory(self):
        """Personality should appear before memory in assembled context."""
        result = build_context(
            role_md="# Lisa",
            memory="MEMORY_MARKER",
            thread_history=[],
            system_docs="",
            personality_md="PERSONALITY_MARKER",
        )
        assert result.index("PERSONALITY_MARKER") < result.index("MEMORY_MARKER")

    def test_full_loading_order(self):
        """Full context should follow order: SOUL -> role -> personality -> memory -> docs -> thread."""
        result = build_context(
            role_md="ROLE_MARKER",
            memory="MEMORY_MARKER",
            thread_history=[{"user": "U0001", "text": "THREAD_MARKER", "ts": "1.0"}],
            system_docs="DOCS_MARKER",
            soul_md="SOUL_MARKER",
            personality_md="PERSONALITY_MARKER",
        )
        positions = {
            "soul": result.index("SOUL_MARKER"),
            "role": result.index("ROLE_MARKER"),
            "personality": result.index("PERSONALITY_MARKER"),
            "memory": result.index("MEMORY_MARKER"),
            "docs": result.index("DOCS_MARKER"),
            "thread": result.index("THREAD_MARKER"),
        }
        assert positions["soul"] < positions["role"]
        assert positions["role"] < positions["personality"]
        assert positions["personality"] < positions["memory"]
        assert positions["memory"] < positions["docs"]
        assert positions["docs"] < positions["thread"]


class TestSoulWithEmptySections:
    """Tests for graceful handling of missing SOUL/personality content."""

    def test_empty_soul_omitted(self):
        """Empty soul_md should not add blank sections to context."""
        result = build_context(
            role_md="# Lisa",
            memory="",
            thread_history=[],
            system_docs="",
            soul_md="",
        )
        assert result.strip() == "# Lisa"

    def test_empty_personality_omitted(self):
        """Empty personality_md should not add blank sections to context."""
        result = build_context(
            role_md="# Lisa",
            memory="",
            thread_history=[],
            system_docs="",
            personality_md="",
        )
        assert result.strip() == "# Lisa"

    def test_whitespace_only_soul_omitted(self):
        """Whitespace-only soul_md should be treated as empty."""
        result = build_context(
            role_md="# Lisa",
            memory="",
            thread_history=[],
            system_docs="",
            soul_md="   \n  ",
        )
        assert result.strip() == "# Lisa"

    def test_backwards_compatible_without_soul_personality(self):
        """Calling build_context without soul/personality should work as before."""
        result = build_context(
            role_md="# Lisa\nSenior developer.",
            memory="## Recent\nFixed auth.",
            thread_history=[{"user": "U0001", "text": "Help", "ts": "1.0"}],
            system_docs="# Outlook\nAPI docs.",
        )
        assert "Lisa" in result
        assert "auth" in result
        assert "Outlook" in result


class TestLoadMemoryFunction:
    """Tests for the load_memory utility from memory_loader."""

    def test_load_existing_file(self, test_memory_dir):
        """Should load content from an existing file."""
        result = load_memory(test_memory_dir / "shared" / "MEMORY.md")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_load_soul_file(self, test_memory_dir):
        """Should load SOUL.md from the shared directory."""
        result = load_memory(test_memory_dir / "shared" / "SOUL.md")
        assert "genuinely helpful" in result

    def test_load_personality_file(self, test_memory_dir):
        """Should load personality.md for a specific agent."""
        result = load_memory(test_memory_dir / "agents" / "lisa" / "personality.md")
        assert "warm" in result.lower()

    def test_load_missing_file(self):
        """Should return empty string for missing files."""
        result = load_memory("/tmp/nonexistent_soul_12345.md")
        assert result == ""


class TestLoadAgentContext:
    """Tests for loading complete agent context in the correct order."""

    def test_load_agent_context_returns_list(self, test_memory_dir):
        """load_agent_context() should return a list of (label, content) tuples."""
        result = load_agent_context(
            agent_name="lisa",
            shared_dir=test_memory_dir / "shared",
            agent_dir=test_memory_dir / "agents" / "lisa",
        )
        assert isinstance(result, list)
        assert all(isinstance(item, tuple) and len(item) == 2 for item in result)

    def test_load_agent_context_includes_soul(self, test_memory_dir):
        """Agent context should include SOUL content."""
        result = load_agent_context(
            agent_name="lisa",
            shared_dir=test_memory_dir / "shared",
            agent_dir=test_memory_dir / "agents" / "lisa",
        )
        labels = [label for label, _ in result]
        assert "soul" in labels

    def test_load_agent_context_includes_personality(self, test_memory_dir):
        """Agent context should include personality content."""
        result = load_agent_context(
            agent_name="lisa",
            shared_dir=test_memory_dir / "shared",
            agent_dir=test_memory_dir / "agents" / "lisa",
        )
        labels = [label for label, _ in result]
        assert "personality" in labels

    def test_load_agent_context_order(self, test_memory_dir):
        """Context should be loaded in order: soul, role, personality, agent_memory, org_memory."""
        result = load_agent_context(
            agent_name="lisa",
            shared_dir=test_memory_dir / "shared",
            agent_dir=test_memory_dir / "agents" / "lisa",
        )
        labels = [label for label, _ in result]
        # Verify relative ordering of present items
        if "soul" in labels and "personality" in labels:
            assert labels.index("soul") < labels.index("personality")
        if "role" in labels and "personality" in labels:
            assert labels.index("role") < labels.index("personality")

    def test_load_agent_context_skips_missing(self, tmp_path):
        """Missing files should be silently skipped."""
        # Empty directories — no files exist
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        agent_dir = tmp_path / "agents" / "lisa"
        agent_dir.mkdir(parents=True)

        result = load_agent_context(
            agent_name="lisa",
            shared_dir=shared_dir,
            agent_dir=agent_dir,
        )
        assert result == []

    def test_no_duplication_between_soul_and_personality(self, test_memory_dir):
        """SOUL content and personality content should not overlap."""
        result = load_agent_context(
            agent_name="lisa",
            shared_dir=test_memory_dir / "shared",
            agent_dir=test_memory_dir / "agents" / "lisa",
        )
        contents = {label: content for label, content in result}
        if "soul" in contents and "personality" in contents:
            # Personality should not repeat SOUL's core rules
            soul_lines = set(contents["soul"].strip().splitlines())
            personality_lines = set(contents["personality"].strip().splitlines())
            # Headers can overlap (e.g. "# "), but content lines should not
            content_overlap = soul_lines & personality_lines - {"", "#"}
            assert len(content_overlap) == 0, f"Duplicated lines: {content_overlap}"
