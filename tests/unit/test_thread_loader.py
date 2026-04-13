"""Unit tests for router.thread_loader — thread history parsing and summary detection.

These tests define the interface that router/thread_loader.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

thread_loader = pytest.importorskip("router.thread_loader", reason="router.thread_loader not yet implemented")

pytestmark = pytest.mark.unit


class TestThreadHistoryParsing:
    """Tests for parsing Slack thread history into a usable format."""

    def test_parse_thread_returns_list(self, sample_thread_history):
        """parse_thread() should return a list of message dicts."""
        result = thread_loader.parse_thread(sample_thread_history)
        assert isinstance(result, list)

    def test_parse_thread_preserves_order(self, sample_thread_history):
        """Parsed messages should maintain chronological order."""
        result = thread_loader.parse_thread(sample_thread_history)
        timestamps = [msg["ts"] for msg in result]
        assert timestamps == sorted(timestamps)

    def test_parse_thread_includes_user_and_text(self, sample_thread_history):
        """Each parsed message should have 'user' and 'text' fields."""
        result = thread_loader.parse_thread(sample_thread_history)
        for msg in result:
            assert "user" in msg
            assert "text" in msg


class TestSummaryDetection:
    """Tests for detecting summary markers in thread history."""

    def test_detect_summary_in_thread(self):
        """Should detect when a thread contains a summary message."""
        messages = [
            {"user": "U_BOT", "text": "## Session Summary\nCompleted auth review.", "ts": "1.0"},
        ]
        assert thread_loader.has_summary(messages) is True

    def test_no_summary_in_regular_thread(self):
        """Should return False when thread has no summary."""
        messages = [
            {"user": "U0001", "text": "Hey can you help?", "ts": "1.0"},
            {"user": "U_BOT", "text": "Sure thing.", "ts": "2.0"},
        ]
        assert thread_loader.has_summary(messages) is False


class TestEmptyThread:
    """Tests for handling empty threads."""

    def test_parse_empty_thread(self):
        """parse_thread() with an empty list should return an empty list."""
        result = thread_loader.parse_thread([])
        assert result == []

    def test_has_summary_empty_thread(self):
        """has_summary() with an empty list should return False."""
        assert thread_loader.has_summary([]) is False
