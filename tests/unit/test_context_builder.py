"""Unit tests for router.context_builder — context assembly and token management.

These tests define the interface that router/context_builder.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

context_builder = pytest.importorskip("router.context_builder", reason="router.context_builder not yet implemented")

pytestmark = pytest.mark.unit


class TestContextAssemblyOrder:
    """Tests for the order in which context components are assembled."""

    def test_build_context_returns_string(self):
        """build_context() should return a string."""
        result = context_builder.build_context(
            role_md="# Lisa\nSenior developer.",
            memory="## Recent sessions\nFixed auth bug.",
            thread_history=[{"user": "U0001", "text": "Help with auth", "ts": "1.0"}],
            system_docs="# Outlook\nCalendar API docs.",
        )
        assert isinstance(result, str)

    def test_context_includes_role(self):
        """Built context should include the role content."""
        role = "# Lisa\nSenior developer."
        result = context_builder.build_context(
            role_md=role,
            memory="",
            thread_history=[],
            system_docs="",
        )
        assert "Lisa" in result

    def test_context_includes_memory(self):
        """Built context should include memory content when provided."""
        memory = "## Session 2024-01-20\nFixed the auth module."
        result = context_builder.build_context(
            role_md="# Lisa",
            memory=memory,
            thread_history=[],
            system_docs="",
        )
        assert "auth module" in result

    def test_context_assembly_order(self):
        """Role should appear before memory, memory before thread history."""
        result = context_builder.build_context(
            role_md="ROLE_MARKER",
            memory="MEMORY_MARKER",
            thread_history=[{"user": "U0001", "text": "THREAD_MARKER", "ts": "1.0"}],
            system_docs="",
        )
        role_pos = result.index("ROLE_MARKER")
        memory_pos = result.index("MEMORY_MARKER")
        thread_pos = result.index("THREAD_MARKER")
        assert role_pos < memory_pos < thread_pos


class TestTokenEstimation:
    """Tests for token count estimation."""

    def test_estimate_tokens_returns_int(self):
        """estimate_tokens() should return an integer."""
        result = context_builder.estimate_tokens("Hello, world!")
        assert isinstance(result, int)

    def test_estimate_tokens_proportional_to_length(self):
        """Longer text should have a higher token estimate."""
        short = context_builder.estimate_tokens("Hello")
        long = context_builder.estimate_tokens("Hello " * 100)
        assert long > short

    def test_estimate_tokens_empty_string(self):
        """Empty string should return 0 tokens."""
        assert context_builder.estimate_tokens("") == 0


class TestTruncation:
    """Tests for context truncation when exceeding token budget."""

    def test_truncate_to_budget(self):
        """truncate_to_budget() should return text within the token budget."""
        long_text = "word " * 10000
        result = context_builder.truncate_to_budget(long_text, max_tokens=100)
        assert context_builder.estimate_tokens(result) <= 100

    def test_truncate_preserves_short_text(self):
        """Text already within budget should not be truncated."""
        short_text = "Hello, world!"
        result = context_builder.truncate_to_budget(short_text, max_tokens=1000)
        assert result == short_text
