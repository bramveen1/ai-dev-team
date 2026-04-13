"""Unit tests for router.context_builder — context assembly and token management."""

import pytest

from router.context_builder import (
    TRUNCATION_MARKER,
    build_context,
    build_conversation_context,
    estimate_tokens,
    truncate_to_budget,
)

pytestmark = pytest.mark.unit


class TestContextAssemblyOrder:
    """Tests for the order in which context components are assembled."""

    def test_build_context_returns_string(self):
        """build_context() should return a string."""
        result = build_context(
            role_md="# Lisa\nSenior developer.",
            memory="## Recent sessions\nFixed auth bug.",
            thread_history=[{"user": "U0001", "text": "Help with auth", "ts": "1.0"}],
            system_docs="# Outlook\nCalendar API docs.",
        )
        assert isinstance(result, str)

    def test_context_includes_role(self):
        """Built context should include the role content."""
        role = "# Lisa\nSenior developer."
        result = build_context(
            role_md=role,
            memory="",
            thread_history=[],
            system_docs="",
        )
        assert "Lisa" in result

    def test_context_includes_memory(self):
        """Built context should include memory content when provided."""
        memory = "## Session 2024-01-20\nFixed the auth module."
        result = build_context(
            role_md="# Lisa",
            memory=memory,
            thread_history=[],
            system_docs="",
        )
        assert "auth module" in result

    def test_context_assembly_order(self):
        """Role should appear before memory, memory before thread history."""
        result = build_context(
            role_md="ROLE_MARKER",
            memory="MEMORY_MARKER",
            thread_history=[{"user": "U0001", "text": "THREAD_MARKER", "ts": "1.0"}],
            system_docs="",
        )
        role_pos = result.index("ROLE_MARKER")
        memory_pos = result.index("MEMORY_MARKER")
        thread_pos = result.index("THREAD_MARKER")
        assert role_pos < memory_pos < thread_pos

    def test_context_with_empty_sections(self):
        """Empty sections should not produce extra whitespace/headers."""
        result = build_context(
            role_md="# Lisa",
            memory="",
            thread_history=[],
            system_docs="",
        )
        assert result.strip() == "# Lisa"

    def test_context_includes_system_docs(self):
        """Built context should include system documentation."""
        result = build_context(
            role_md="# Lisa",
            memory="",
            thread_history=[],
            system_docs="# Outlook\nCalendar API docs.",
        )
        assert "Outlook" in result


class TestTokenEstimation:
    """Tests for token count estimation."""

    def test_estimate_tokens_returns_int(self):
        """estimate_tokens() should return an integer."""
        result = estimate_tokens("Hello, world!")
        assert isinstance(result, int)

    def test_estimate_tokens_proportional_to_length(self):
        """Longer text should have a higher token estimate."""
        short = estimate_tokens("Hello")
        long = estimate_tokens("Hello " * 100)
        assert long > short

    def test_estimate_tokens_empty_string(self):
        """Empty string should return 0 tokens."""
        assert estimate_tokens("") == 0

    def test_estimate_tokens_rough_accuracy(self):
        """Token estimate should be roughly chars / 4."""
        text = "a" * 400
        assert estimate_tokens(text) == 100


class TestTruncation:
    """Tests for context truncation when exceeding token budget."""

    def test_truncate_to_budget(self):
        """truncate_to_budget() should return text within the token budget."""
        long_text = "word " * 10000
        result = truncate_to_budget(long_text, max_tokens=100)
        assert estimate_tokens(result) <= 100

    def test_truncate_preserves_short_text(self):
        """Text already within budget should not be truncated."""
        short_text = "Hello, world!"
        result = truncate_to_budget(short_text, max_tokens=1000)
        assert result == short_text

    def test_truncate_adds_marker(self):
        """Truncated text should include a truncation marker."""
        long_text = "word " * 10000
        result = truncate_to_budget(long_text, max_tokens=100)
        assert TRUNCATION_MARKER in result

    def test_truncate_keeps_recent_content(self):
        """Truncation should keep the most recent (tail) content."""
        lines = [f"Line {i}" for i in range(100)]
        text = "\n".join(lines)
        result = truncate_to_budget(text, max_tokens=50)
        # The last line should be preserved
        assert "Line 99" in result


class TestBuildConversationContext:
    """Tests for the conversation transcript builder."""

    def test_empty_history_returns_empty(self):
        """Empty thread history should return empty string."""
        assert build_conversation_context([]) == ""

    def test_formats_user_messages(self):
        """User messages should be labeled with User(id)."""
        history = [{"user": "U0001", "text": "Hello", "ts": "1.0"}]
        result = build_conversation_context(history)
        assert "[User(U0001)]" in result
        assert "Hello" in result

    def test_formats_bot_messages_with_agent_name(self):
        """Bot messages should be labeled with the agent name."""
        history = [{"user": "U_BOT", "text": "Hi there", "ts": "1.0"}]
        result = build_conversation_context(history, agent_name="Lisa")
        assert "[Lisa]" in result
        assert "Hi there" in result

    def test_bot_user_id_matching(self):
        """When bot_user_id is provided, matching messages get agent label."""
        history = [
            {"user": "U12345", "text": "I am the bot", "ts": "1.0"},
            {"user": "U0001", "text": "I am a human", "ts": "2.0"},
        ]
        result = build_conversation_context(history, bot_user_id="U12345", agent_name="Lisa")
        assert "[Lisa]: I am the bot" in result
        assert "[User(U0001)]: I am a human" in result

    def test_multi_turn_transcript(self):
        """Multi-turn conversation should be formatted line by line."""
        history = [
            {"user": "U0001", "text": "Can you check my calendar?", "ts": "1.0"},
            {"user": "U_BOT", "text": "You have 3 meetings tomorrow.", "ts": "2.0"},
            {"user": "U0001", "text": "Move the 2pm to Thursday.", "ts": "3.0"},
        ]
        result = build_conversation_context(history, agent_name="Lisa")
        lines = result.split("\n")
        assert len(lines) == 3
        assert "[Lisa]" in lines[1]

    def test_multiple_participants(self):
        """Thread with multiple human participants should label each correctly."""
        history = [
            {"user": "U0001", "text": "Hey Lisa", "ts": "1.0"},
            {"user": "U0002", "text": "I have a question too", "ts": "2.0"},
            {"user": "U_BOT", "text": "Sure, how can I help?", "ts": "3.0"},
        ]
        result = build_conversation_context(history, agent_name="Lisa")
        assert "User(U0001)" in result
        assert "User(U0002)" in result
        assert "[Lisa]" in result
