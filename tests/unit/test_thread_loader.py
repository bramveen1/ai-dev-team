"""Unit tests for router.thread_loader — thread history parsing and summary detection.

These tests define the interface that router/thread_loader.py must implement.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.thread_loader import (
    find_session_summary,
    has_summary,
    load_thread_history,
    parse_thread,
    split_messages_at_summary,
)

pytestmark = pytest.mark.unit


class TestThreadHistoryParsing:
    """Tests for parsing Slack thread messages into a usable format."""

    def test_parse_thread_returns_list(self, sample_thread_history):
        """parse_thread() should return a list of message dicts."""
        result = parse_thread(sample_thread_history)
        assert isinstance(result, list)

    def test_parse_thread_preserves_order(self, sample_thread_history):
        """Parsed messages should maintain chronological order."""
        result = parse_thread(sample_thread_history)
        timestamps = [msg["ts"] for msg in result]
        assert timestamps == sorted(timestamps)

    def test_parse_thread_includes_user_and_text(self, sample_thread_history):
        """Each parsed message should have 'user' and 'text' fields."""
        result = parse_thread(sample_thread_history)
        for msg in result:
            assert "user" in msg
            assert "text" in msg

    def test_parse_thread_filters_system_messages(self):
        """System messages (join/leave) should be filtered out."""
        messages = [
            {"user": "U0001", "text": "Hello", "ts": "1.0"},
            {"subtype": "channel_join", "user": "U0002", "text": "joined", "ts": "2.0"},
            {"user": "U0001", "text": "How are you?", "ts": "3.0"},
            {"subtype": "channel_leave", "user": "U0003", "text": "left", "ts": "4.0"},
        ]
        result = parse_thread(messages)
        assert len(result) == 2
        assert result[0]["text"] == "Hello"
        assert result[1]["text"] == "How are you?"

    def test_parse_thread_filters_empty_text(self):
        """Messages with empty or whitespace-only text should be filtered."""
        messages = [
            {"user": "U0001", "text": "", "ts": "1.0"},
            {"user": "U0001", "text": "   ", "ts": "2.0"},
            {"user": "U0001", "text": "Real message", "ts": "3.0"},
        ]
        result = parse_thread(messages)
        assert len(result) == 1
        assert result[0]["text"] == "Real message"

    def test_parse_thread_uses_bot_id_when_no_user(self):
        """Messages without a user field should fall back to bot_id."""
        messages = [
            {"bot_id": "B001", "text": "Bot message", "ts": "1.0"},
        ]
        result = parse_thread(messages)
        assert len(result) == 1
        assert result[0]["user"] == "B001"


class TestSummaryDetection:
    """Tests for detecting summary markers in thread history."""

    def test_detect_summary_in_thread(self):
        """Should detect when a thread contains a summary message."""
        messages = [
            {"user": "U_BOT", "text": "## Session Summary\nCompleted auth review.", "ts": "1.0"},
        ]
        assert has_summary(messages) is True

    def test_no_summary_in_regular_thread(self):
        """Should return False when thread has no summary."""
        messages = [
            {"user": "U0001", "text": "Hey can you help?", "ts": "1.0"},
            {"user": "U_BOT", "text": "Sure thing.", "ts": "2.0"},
        ]
        assert has_summary(messages) is False


class TestEmptyThread:
    """Tests for handling empty threads."""

    def test_parse_empty_thread(self):
        """parse_thread() with an empty list should return an empty list."""
        result = parse_thread([])
        assert result == []

    def test_has_summary_empty_thread(self):
        """has_summary() with an empty list should return False."""
        assert has_summary([]) is False


class TestLoadThreadHistory:
    """Tests for the async load_thread_history function."""

    @pytest.mark.asyncio
    async def test_load_thread_history_returns_parsed_messages(self):
        """load_thread_history should fetch and parse thread messages."""
        client = MagicMock()
        client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [
                    {"user": "U0001", "text": "Hello", "ts": "1.0"},
                    {"user": "U_BOT", "text": "Hi there", "ts": "2.0"},
                ],
            }
        )
        result = await load_thread_history(client, "C0001", "1.0")
        assert len(result) == 2
        assert result[0]["text"] == "Hello"
        assert result[1]["text"] == "Hi there"

    @pytest.mark.asyncio
    async def test_load_thread_history_respects_max_messages(self):
        """Should return at most max_messages results."""
        messages = [{"user": "U0001", "text": f"msg {i}", "ts": str(float(i))} for i in range(30)]
        client = MagicMock()
        client.conversations_replies = AsyncMock(return_value={"ok": True, "messages": messages})

        result = await load_thread_history(client, "C0001", "0.0", max_messages=5)
        assert len(result) == 5
        # Should be the most recent 5
        assert result[0]["text"] == "msg 25"

    @pytest.mark.asyncio
    async def test_load_thread_history_handles_api_error(self):
        """Should return empty list on Slack API error."""
        client = MagicMock()
        client.conversations_replies = AsyncMock(side_effect=Exception("API error"))

        result = await load_thread_history(client, "C0001", "1.0")
        assert result == []

    @pytest.mark.asyncio
    async def test_load_thread_history_handles_not_ok_response(self):
        """Should return empty list when Slack returns ok=False."""
        client = MagicMock()
        client.conversations_replies = AsyncMock(return_value={"ok": False})

        result = await load_thread_history(client, "C0001", "1.0")
        assert result == []

    @pytest.mark.asyncio
    async def test_load_thread_history_first_message_no_thread(self):
        """Single message (first in thread) should work correctly."""
        client = MagicMock()
        client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [
                    {"user": "U0001", "text": "First message", "ts": "1.0"},
                ],
            }
        )
        result = await load_thread_history(client, "C0001", "1.0")
        assert len(result) == 1
        assert result[0]["text"] == "First message"


class TestFindSessionSummary:
    """Tests for find_session_summary function."""

    def test_finds_most_recent_summary(self):
        """Should return the most recent summary message."""
        messages = [
            {"user": "U_BOT", "text": "regular message", "ts": "1.0"},
            {"user": "U_BOT", "text": "_Session paused. Topic: auth_", "ts": "2.0"},
            {"user": "U0001", "text": "follow up", "ts": "3.0"},
        ]
        result = find_session_summary(messages)
        assert result is not None
        assert "_Session paused" in result

    def test_returns_none_when_no_summary(self):
        """Should return None when no summary is found."""
        messages = [
            {"user": "U0001", "text": "Hello", "ts": "1.0"},
            {"user": "U_BOT", "text": "Hi there", "ts": "2.0"},
        ]
        assert find_session_summary(messages) is None

    def test_returns_none_for_empty_messages(self):
        """Should return None for empty message list."""
        assert find_session_summary([]) is None

    def test_filters_by_bot_user_id(self):
        """When bot_user_id is specified, only match that user's messages."""
        messages = [
            {"user": "U0001", "text": "_Session paused. Topic: auth_", "ts": "1.0"},
            {"user": "U_BOT", "text": "regular message", "ts": "2.0"},
        ]
        # Summary is from U0001, but we filter for U_BOT
        result = find_session_summary(messages, bot_user_id="U_BOT")
        assert result is None

    def test_finds_summary_with_bot_filter(self):
        """When bot_user_id matches, should find the summary."""
        messages = [
            {"user": "U_BOT", "text": "_Session paused. Topic: auth_", "ts": "1.0"},
        ]
        result = find_session_summary(messages, bot_user_id="U_BOT")
        assert result is not None

    def test_finds_session_summary_marker(self):
        """Should detect ## Session Summary marker."""
        messages = [
            {"user": "U_BOT", "text": "## Session Summary\nDone.", "ts": "1.0"},
        ]
        result = find_session_summary(messages)
        assert "Session Summary" in result


class TestSplitMessagesAtSummary:
    """Tests for split_messages_at_summary function."""

    def test_split_at_summary(self):
        """Should split messages at the summary, returning summary and recent messages."""
        messages = [
            {"user": "U0001", "text": "old message", "ts": "1.0"},
            {"user": "U_BOT", "text": "_Session paused. Summary here._", "ts": "2.0"},
            {"user": "U0001", "text": "new message after", "ts": "3.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary is not None
        assert "_Session paused" in summary
        assert len(recent) == 1
        assert recent[0]["text"] == "new message after"

    def test_no_summary_returns_all_messages(self):
        """When there's no summary, return (None, all_messages)."""
        messages = [
            {"user": "U0001", "text": "hello", "ts": "1.0"},
            {"user": "U_BOT", "text": "hi", "ts": "2.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary is None
        assert len(recent) == 2

    def test_empty_messages(self):
        """Empty message list should return (None, [])."""
        summary, recent = split_messages_at_summary([])
        assert summary is None
        assert recent == []

    def test_split_with_bot_user_id_filter(self):
        """Should only consider summaries from specified bot user."""
        messages = [
            {"user": "U0001", "text": "_Session paused. Not from bot._", "ts": "1.0"},
            {"user": "U_BOT", "text": "regular message", "ts": "2.0"},
            {"user": "U0001", "text": "follow up", "ts": "3.0"},
        ]
        summary, recent = split_messages_at_summary(messages, bot_user_id="U_BOT")
        assert summary is None  # The summary marker isn't from U_BOT
        assert len(recent) == 3

    def test_split_uses_latest_summary(self):
        """If multiple summaries exist, split at the latest one."""
        messages = [
            {"user": "U_BOT", "text": "_Session paused. First._", "ts": "1.0"},
            {"user": "U0001", "text": "middle", "ts": "2.0"},
            {"user": "U_BOT", "text": "_Session paused. Second._", "ts": "3.0"},
            {"user": "U0001", "text": "after second", "ts": "4.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert "Second" in summary
        assert len(recent) == 1
        assert recent[0]["text"] == "after second"
