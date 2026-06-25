"""Unit tests for router.thread_loader — thread history parsing and summary detection.

These tests define the interface that router/thread_loader.py must implement.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.thread_loader import (
    HARNESS_SUMMARY_EVENT_TYPE,
    find_session_summary,
    has_summary,
    load_thread_history,
    parse_thread,
    split_messages_at_summary,
)

# Helper used in tests that exercise the provenance-gated split path.
_HARNESS_META = {"event_type": HARNESS_SUMMARY_EVENT_TYPE, "event_payload": {}}

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
    async def test_load_thread_history_requests_message_metadata(self):
        """conversations.replies MUST be called with include_all_metadata=True.

        Slack omits per-message ``metadata`` from the response unless this flag
        is set, which would make Guard 2 provenance detection
        (_is_provenance_verified_summary) always fail in production even though
        the unit tests inject metadata directly. Guards against silent regression
        of the #547/#548 fetch fix.
        """
        client = MagicMock()
        client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [{"user": "U0001", "text": "Hello", "ts": "1.0"}],
            }
        )
        await load_thread_history(client, "C0001", "1.0")
        client.conversations_replies.assert_awaited()
        _, kwargs = client.conversations_replies.await_args
        assert kwargs.get("include_all_metadata") is True

    @pytest.mark.asyncio
    async def test_load_thread_history_respects_max_messages(self):
        """Should return the most recent max_messages, paginating oldest-first.

        Simulates Slack's oldest-first pagination: page 1 returns msgs 0-19
        with a next_cursor, page 2 returns msgs 20-29 with no cursor.
        With max_messages=5 the function must page to the end and return
        only msgs 25-29 — NOT msgs 0-4 from the first page.
        """
        page1_msgs = [{"user": "U0001", "text": f"msg {i}", "ts": str(float(i))} for i in range(20)]
        page2_msgs = [{"user": "U0001", "text": f"msg {i}", "ts": str(float(i))} for i in range(20, 30)]

        page1_response = {"ok": True, "messages": page1_msgs, "response_metadata": {"next_cursor": "cursor_page2"}}
        page2_response = {"ok": True, "messages": page2_msgs, "response_metadata": {"next_cursor": ""}}

        client = MagicMock()
        client.conversations_replies = AsyncMock(side_effect=[page1_response, page2_response])

        result = await load_thread_history(client, "C0001", "0.0", max_messages=5)
        assert len(result) == 5
        # Should be the most recent 5 (from page 2)
        assert result[0]["text"] == "msg 25"
        assert result[-1]["text"] == "msg 29"
        # Both pages must have been fetched
        assert client.conversations_replies.await_count == 2

    @pytest.mark.asyncio
    async def test_load_thread_history_pagination_finds_summary_on_second_page(self):
        """Summary appearing only after the first page must be found after full pagination.

        Without full pagination split_messages_at_summary would never see
        the summary and would treat the whole thread as a fresh session.
        """
        from router.thread_loader import split_messages_at_summary

        # Use ts values with uniform widths so string-sort matches numeric sort
        page1_msgs = [{"user": "U0001", "text": f"old msg {i}", "ts": f"{100 + i}.0"} for i in range(10)]
        page2_msgs = [
            {
                "user": "U_BOT",
                "text": "_Session paused. Summary of old work._",
                "ts": "200.0",
                "metadata": _HARNESS_META,
            },
            {"user": "U0001", "text": "resumed after summary", "ts": "201.0"},
        ]

        page1_response = {"ok": True, "messages": page1_msgs, "response_metadata": {"next_cursor": "cursor_p2"}}
        page2_response = {"ok": True, "messages": page2_msgs, "response_metadata": {"next_cursor": ""}}

        client = MagicMock()
        client.conversations_replies = AsyncMock(side_effect=[page1_response, page2_response])

        history = await load_thread_history(client, "C0001", "0.0", max_messages=20)
        summary, recent = split_messages_at_summary(history)

        assert summary is not None, "summary from second page was not found"
        assert "_Session paused" in summary
        assert len(recent) == 1
        assert recent[0]["text"] == "resumed after summary"

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
    async def test_load_thread_history_short_circuits_on_empty_thread_ts(self):
        """Top-level dispatches (e.g. scheduled tasks) have no thread.

        Calling Slack with ts="" returns thread_not_found, which is just
        log noise — return [] without hitting the API.
        """
        client = MagicMock()
        client.conversations_replies = AsyncMock()

        result = await load_thread_history(client, "C0001", "")
        assert result == []
        client.conversations_replies.assert_not_awaited()

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
        """Should split at a provenance-verified harness summary."""
        messages = [
            {"user": "U0001", "text": "old message", "ts": "1.0"},
            {"user": "U_BOT", "text": "_Session paused. Summary here._", "ts": "2.0", "metadata": _HARNESS_META},
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
        """If multiple provenance-verified summaries exist, split at the latest one."""
        messages = [
            {"user": "U_BOT", "text": "_Session paused. First._", "ts": "1.0", "metadata": _HARNESS_META},
            {"user": "U0001", "text": "middle", "ts": "2.0"},
            {"user": "U_BOT", "text": "_Session paused. Second._", "ts": "3.0", "metadata": _HARNESS_META},
            {"user": "U0001", "text": "after second", "ts": "4.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert "Second" in summary
        assert len(recent) == 1
        assert recent[0]["text"] == "after second"


class TestFallbackSummaryRoundTrip:
    """Tests that the deterministic fallback summary produced by session_end
    is correctly detected as a summary boundary by thread_loader."""

    def test_fallback_summary_detected_by_has_summary(self):
        """has_summary should find a fallback summary as a summary message."""
        from router.session_end import _build_fallback_summary

        history = [{"user": "U001", "text": "some work was done"}]
        fallback_text = _build_fallback_summary(history)

        messages = [{"user": "U_BOT", "text": fallback_text, "ts": "1.0"}]
        assert has_summary(messages) is True

    def test_fallback_summary_found_by_find_session_summary(self):
        """find_session_summary should return the fallback text."""
        from router.session_end import _build_fallback_summary

        history = [{"user": "U001", "text": "discussing auth"}]
        fallback_text = _build_fallback_summary(history)

        messages = [{"user": "U_BOT", "text": fallback_text, "ts": "1.0"}]
        result = find_session_summary(messages)
        assert result == fallback_text

    def test_fallback_summary_acts_as_split_boundary(self):
        """split_messages_at_summary must split at the fallback summary when it carries
        the harness provenance flag, preserving messages posted after it."""
        from router.session_end import _build_fallback_summary

        history = [{"user": "U001", "text": "first task"}]
        fallback_text = _build_fallback_summary(history)

        messages = [
            {"user": "U001", "text": "old message before summary", "ts": "1.0"},
            {"user": "U_BOT", "text": fallback_text, "ts": "2.0", "metadata": _HARNESS_META},
            {"user": "U001", "text": "new message after summary", "ts": "3.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary == fallback_text
        assert len(recent) == 1
        assert recent[0]["text"] == "new message after summary"


class TestGuard2ProvenanceGating:
    """Guard 2 (issue #547): split_messages_at_summary only truncates on harness-authored
    summary blocks identified by the provenance flag — not arbitrary text."""

    def test_peer_summary_text_does_not_truncate_live_thread(self):
        """A peer message containing ## Session Summary must NOT truncate the thread.

        This is the exact bug from the issue: Lin posts a session summary into
        Sam's thread. Without Guard 2, split_messages_at_summary would treat it
        as a real boundary and nuke the live thread.
        """
        messages = [
            {"user": "U_SAM", "text": "working on auth", "ts": "1.0"},
            {"user": "U_LIN", "text": "## Session Summary\nCompleted billing work.", "ts": "2.0"},
            {"user": "U_SAM", "text": "follow-up after Lin's post", "ts": "3.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary is None, "peer summary must not create a session boundary"
        assert len(recent) == 3, "live thread must remain intact"

    def test_peer_summary_text_not_surfaced_as_previous_session_summary(self):
        """The peer summary text must not be returned as a PREVIOUS SESSION SUMMARY."""
        messages = [
            {"user": "U_LIN", "text": "_Session paused. Lin's own summary._", "ts": "1.0"},
            {"user": "U_SAM", "text": "real question", "ts": "2.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary is None
        assert len(recent) == 2

    def test_harness_summary_with_flag_does_truncate(self):
        """A genuine harness-authored summary (provenance flag set) DOES truncate.

        Regression guard: removing the provenance flag must restore the old
        (truncating) behaviour so the session-resume path keeps working.
        """
        messages = [
            {"user": "U_SAM", "text": "old work", "ts": "1.0"},
            {
                "user": "U_SAM_BOT",
                "text": "_Session paused. Summary of old work._",
                "ts": "2.0",
                "metadata": _HARNESS_META,
            },
            {"user": "U_SAM", "text": "resumed after pause", "ts": "3.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary is not None, "harness summary with provenance flag must create a boundary"
        assert "_Session paused" in summary
        assert len(recent) == 1
        assert recent[0]["text"] == "resumed after pause"

    def test_missing_provenance_flag_fails_safe(self):
        """A legacy summary block without the provenance flag fails safe (no truncation).

        This covers the upgrade path: summaries posted before this fix lack the
        metadata flag. They should not truncate the thread (safe degradation) rather
        than blindly trusting the substring (which would re-open the peer-summary bug).
        """
        messages = [
            {"user": "U_SAM", "text": "old work", "ts": "1.0"},
            {"user": "U_SAM_BOT", "text": "_Session paused. Legacy summary._", "ts": "2.0"},
            {"user": "U_SAM", "text": "new message", "ts": "3.0"},
        ]
        summary, recent = split_messages_at_summary(messages)
        assert summary is None, "legacy summary without provenance flag must not truncate"
        assert len(recent) == 3
