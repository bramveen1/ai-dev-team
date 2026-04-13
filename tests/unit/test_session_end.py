"""Unit tests for router.session_end — clean exit trigger detection and memory extraction.

These tests define the interface that router/session_end.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

session_end = pytest.importorskip("router.session_end", reason="router.session_end not yet implemented")

pytestmark = pytest.mark.unit


class TestCleanExitTriggerDetection:
    """Tests for detecting clean exit triggers in messages."""

    @pytest.mark.parametrize(
        "message",
        [
            "thanks",
            "Thanks!",
            "thank you",
            "cheers",
            "Cheers!",
            "that's all",
            "That's all, thanks",
            "looks good, thanks!",
        ],
    )
    def test_detects_exit_triggers(self, message):
        """Should detect common exit trigger phrases."""
        assert session_end.is_exit_trigger(message) is True

    def test_trigger_is_case_insensitive(self):
        """Exit trigger detection should be case-insensitive."""
        assert session_end.is_exit_trigger("THANKS") is True
        assert session_end.is_exit_trigger("Thanks") is True
        assert session_end.is_exit_trigger("tHaNkS") is True
        assert session_end.is_exit_trigger("CHEERS") is True

    @pytest.mark.parametrize(
        "message",
        [
            "Can you fix the auth module?",
            "Please review this PR",
            "What does this function do?",
            "Let's refactor the database layer",
        ],
    )
    def test_non_exit_messages(self, message):
        """Regular work messages should not trigger exit."""
        assert session_end.is_exit_trigger(message) is False

    def test_empty_message_not_trigger(self):
        """An empty message should not be an exit trigger."""
        assert session_end.is_exit_trigger("") is False


class TestMemoryExtractionParsing:
    """Tests for parsing memory extraction from agent responses."""

    def test_extract_memory_from_response(self):
        """Should extract memory block from a structured agent response."""
        response = (
            "I've completed the auth review.\n\n"
            "## Memory\n"
            "- Reviewed auth module, found 2 issues\n"
            "- Suggested rate limiting addition\n"
        )
        result = session_end.extract_memory(response)
        assert "auth module" in result

    def test_extract_memory_no_block(self):
        """Should return empty string when no memory block is present."""
        response = "Done! The auth module looks good."
        result = session_end.extract_memory(response)
        assert result == ""

    def test_extract_memory_empty_response(self):
        """Should handle empty response gracefully."""
        result = session_end.extract_memory("")
        assert result == ""
