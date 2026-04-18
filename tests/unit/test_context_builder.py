"""Unit tests for router.context_builder — context assembly and token management."""

import pytest

from router.context_builder import (
    TRUNCATION_MARKER,
    _truncate_context,
    build_context,
    build_conversation_context,
    build_full_context,
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

    def test_multi_agent_transcript_via_bot_user_map(self):
        """With multiple agents in a thread, each bot user ID maps to its
        own agent label."""
        history = [
            {"user": "U0001", "text": "hi", "ts": "1.0"},
            {"user": "U_BOT_LISA", "text": "Lisa here", "ts": "2.0"},
            {"user": "U_BOT_SAM", "text": "Sam joining", "ts": "3.0"},
            {"user": "U0001", "text": "thanks all", "ts": "4.0"},
        ]
        result = build_conversation_context(
            history,
            agent_name="Sam",
            bot_user_map={"U_BOT_LISA": "Lisa", "U_BOT_SAM": "Sam"},
        )
        assert "[Lisa]: Lisa here" in result
        assert "[Sam]: Sam joining" in result
        assert "[User(U0001)]: hi" in result

    def test_bot_user_map_honours_agent_name_aliases(self):
        """When messages are tagged with agent names (from router's session
        log), the transcript should label them with the mapped display name."""
        history = [
            {"user": "lisa", "text": "I answered", "ts": "1.0"},
        ]
        result = build_conversation_context(
            history,
            agent_name="Sam",
            bot_user_map={"U_BOT_LISA": "Lisa"},
        )
        assert "[Lisa]: I answered" in result


class TestTruncateToZeroBudget:
    """Edge case: truncation with extremely small budget."""

    def test_truncate_returns_marker_only(self):
        """When budget is zero, should return just the truncation marker."""
        result = truncate_to_budget("some long text " * 100, max_tokens=0)
        assert TRUNCATION_MARKER in result


class TestBuildFullContext:
    """Tests for build_full_context function."""

    def test_includes_org_memory(self):
        """Should include organizational memory section."""
        memory = {"org_memory": "Org rules here", "agent_memory": "", "system_docs": []}
        result = build_full_context(memory=memory, thread_history=[], new_message="hi")
        assert "ORGANIZATIONAL MEMORY" in result
        assert "Org rules here" in result

    def test_includes_agent_memory(self):
        """Should include agent memory section."""
        memory = {"org_memory": "", "agent_memory": "Agent notes", "system_docs": []}
        result = build_full_context(memory=memory, thread_history=[], new_message="hi", agent_name="lisa")
        assert "YOUR MEMORY" in result
        assert "Agent notes" in result

    def test_includes_system_docs(self):
        """Should include system docs section."""
        memory = {"org_memory": "", "agent_memory": "", "system_docs": ["# Outlook API\nDocs here"]}
        result = build_full_context(memory=memory, thread_history=[], new_message="hi")
        assert "TOOL DOCUMENTATION" in result
        assert "Outlook API" in result

    def test_includes_session_summary(self):
        """Should include session summary when provided."""
        memory = {"org_memory": "", "agent_memory": "", "system_docs": []}
        result = build_full_context(
            memory=memory,
            thread_history=[{"user": "U001", "text": "follow up", "ts": "2.0"}],
            new_message="hi",
            session_summary="Previous session: auth review",
        )
        assert "PREVIOUS SESSION SUMMARY" in result
        assert "auth review" in result
        assert "RECENT MESSAGES" in result

    def test_includes_new_message(self):
        """Should include the new message section."""
        memory = {"org_memory": "", "agent_memory": "", "system_docs": []}
        result = build_full_context(memory=memory, thread_history=[], new_message="Hello Lisa")
        assert "NEW MESSAGE" in result
        assert "Hello Lisa" in result

    def test_includes_conversation_history(self):
        """Should include conversation history without session summary."""
        memory = {"org_memory": "", "agent_memory": "", "system_docs": []}
        history = [{"user": "U001", "text": "question", "ts": "1.0"}]
        result = build_full_context(memory=memory, thread_history=history, new_message="follow up")
        assert "CONVERSATION HISTORY" in result

    def test_truncates_when_over_budget(self):
        """Should truncate context when exceeding token budget."""
        memory = {
            "org_memory": "x" * 1000,
            "agent_memory": "y" * 1000,
            "system_docs": ["z" * 1000],
        }
        history = [{"user": "U001", "text": "w" * 1000, "ts": "1.0"}]
        result = build_full_context(
            memory=memory,
            thread_history=history,
            new_message="hello",
            max_tokens=100,
        )
        # Should have dropped conversation history to fit
        assert "CONVERSATION HISTORY" not in result

    def test_empty_sections_omitted(self):
        """Empty memory sections should not appear in output."""
        memory = {"org_memory": "", "agent_memory": "", "system_docs": []}
        result = build_full_context(memory=memory, thread_history=[], new_message="hi")
        assert "ORGANIZATIONAL MEMORY" not in result
        assert "YOUR MEMORY" not in result
        assert "TOOL DOCUMENTATION" not in result

    def test_agent_name_in_header(self):
        """Agent name should be uppercased in the memory header."""
        memory = {"org_memory": "", "agent_memory": "notes", "system_docs": []}
        result = build_full_context(memory=memory, thread_history=[], new_message="hi", agent_name="lisa")
        assert "LISA" in result

    def test_default_agent_name(self):
        """When no agent name, should use AGENT."""
        memory = {"org_memory": "", "agent_memory": "notes", "system_docs": []}
        result = build_full_context(memory=memory, thread_history=[], new_message="hi")
        assert "AGENT" in result


class TestTruncateContext:
    """Tests for _truncate_context helper."""

    def test_drops_thread_history_first(self):
        """Should drop conversation history first."""
        sections = [
            "--- ORGANIZATIONAL MEMORY ---\norg info",
            "--- CONVERSATION HISTORY ---\n" + "x" * 10000,
            "--- NEW MESSAGE ---\nhello",
        ]
        result = _truncate_context(sections, max_tokens=100)
        assert "CONVERSATION HISTORY" not in result
        assert "hello" in result

    def test_drops_system_docs_second(self):
        """Should drop system docs if still over budget after dropping history."""
        sections = [
            "--- ORGANIZATIONAL MEMORY ---\n" + "x" * 1000,
            "--- TOOL DOCUMENTATION ---\n" + "y" * 5000,
            "--- NEW MESSAGE ---\nhello",
        ]
        result = _truncate_context(sections, max_tokens=300)
        assert "TOOL DOCUMENTATION" not in result

    def test_hard_truncate_as_last_resort(self):
        """If still over budget, should hard-truncate."""
        sections = [
            "--- ORGANIZATIONAL MEMORY ---\n" + "x" * 50000,
            "--- NEW MESSAGE ---\nhello",
        ]
        result = _truncate_context(sections, max_tokens=50)
        assert estimate_tokens(result) <= 50
