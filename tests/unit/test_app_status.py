"""Unit tests for assistant thread status indicator.

Verifies that the router sets the assistant thread status while the agent
works, and that the status auto-clears when the response is posted.
"""

from unittest.mock import AsyncMock, patch

import pytest

from router.app import (
    DEFAULT_THINKING_STATUS,
    _handle_event,
    set_assistant_status,
)

pytestmark = pytest.mark.unit

FINAL_RESPONSE = "Here's my analysis of the auth module."


# ── Helper functions ────────────────────────────────────────────────


class TestSetAssistantStatus:
    """Tests for the set_assistant_status helper."""

    @pytest.mark.asyncio
    async def test_calls_assistant_threads_set_status(self, mock_slack_client):
        await set_assistant_status(mock_slack_client, "C0001", "1.0", "is thinking\u2026")
        mock_slack_client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="C0001",
            thread_ts="1.0",
            status="is thinking\u2026",
        )

    @pytest.mark.asyncio
    async def test_swallows_errors(self, mock_slack_client):
        """Status is non-critical — errors should not propagate."""
        mock_slack_client.assistant_threads_setStatus = AsyncMock(side_effect=Exception("not_allowed"))
        await set_assistant_status(mock_slack_client, "C0001", "1.0", "is thinking\u2026")


# ── Status lifecycle in _handle_event ──────────────────────────────


def _make_event(*, channel="C0001", user="U0001", text="Hello Lisa", ts="1705700000.000100", thread_ts=None, **kw):
    """Build a minimal Slack event dict for testing."""
    evt = {
        "type": "app_mention",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        evt["thread_ts"] = thread_ts
    evt.update(kw)
    return evt


@pytest.fixture()
def mock_dispatch():
    with patch("router.app.dispatch", new_callable=AsyncMock) as mock:
        mock.return_value = {"agent": "lisa", "status": "ok", "response": FINAL_RESPONSE}
        yield mock


@pytest.fixture()
def mock_session():
    with patch("router.app.create_session") as cs, patch("router.app.update_activity"):
        cs.return_value = {"session_id": "test-session"}
        yield cs


@pytest.fixture()
def mock_exit_trigger():
    with patch("router.app.is_exit_trigger", return_value=False):
        yield


class TestHandleEventStatus:
    """Tests for the assistant status indicator flow in _handle_event."""

    @pytest.mark.asyncio
    async def test_sets_assistant_status_before_dispatch(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """Assistant status should be set before the agent runs."""
        say = AsyncMock()
        await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.assistant_threads_setStatus.assert_called_once()
        call_kwargs = mock_slack_client.assistant_threads_setStatus.call_args[1]
        assert call_kwargs["status"] == "is reviewing findings\u2026"
        assert call_kwargs["channel_id"] == "C0001"

    @pytest.mark.asyncio
    async def test_posts_response_via_say(self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger):
        """The response should be posted as a normal message via say()."""
        say = AsyncMock()
        await _handle_event(_make_event(), say, mock_slack_client)

        say.assert_called_once()
        assert say.call_args[1]["text"] == FINAL_RESPONSE

    @pytest.mark.asyncio
    async def test_no_placeholder_message_posted(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """No placeholder message should be posted — we use the status API instead."""
        say = AsyncMock()
        await _handle_event(_make_event(), say, mock_slack_client)

        # chat_postMessage should NOT be called for a status placeholder
        mock_slack_client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_chat_update_or_delete_called(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """No message update or delete should happen — status auto-clears."""
        say = AsyncMock()
        await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.chat_update.assert_not_called()
        mock_slack_client.chat_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_reactions_add_called(self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger):
        """No reaction emoji should be used."""
        say = AsyncMock()
        await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.reactions_add.assert_not_called()


class TestHandleEventStatusErrors:
    """Tests for error handling with the status indicator."""

    @pytest.mark.asyncio
    async def test_dispatch_error_posts_error_via_say(self, mock_slack_client, mock_session, mock_exit_trigger):
        """On dispatch failure, error message should be posted via say()."""
        say = AsyncMock()

        with patch("router.app.dispatch", new_callable=AsyncMock, side_effect=RuntimeError("CLI crashed")):
            await _handle_event(_make_event(), say, mock_slack_client)

        say.assert_called_once()
        assert "something went wrong" in say.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_status_failure_does_not_block_dispatch(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """If setting status fails, dispatch should still proceed."""
        mock_slack_client.assistant_threads_setStatus = AsyncMock(side_effect=Exception("scope error"))
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        # Dispatch still happened and response was posted
        mock_dispatch.assert_called_once()
        say.assert_called_once()
        assert say.call_args[1]["text"] == FINAL_RESPONSE


class TestDefaultThinkingStatus:
    """Tests for configurable thinking status text."""

    def test_default_thinking_status_exists(self):
        """DEFAULT_THINKING_STATUS should be defined as a fallback."""
        assert DEFAULT_THINKING_STATUS == "is thinking\u2026"

    def test_lisa_has_custom_thinking_status(self):
        """Lisa's agent config should have a custom thinking_status."""
        from router.config import get_agent_map

        agent_map = get_agent_map()
        assert agent_map["lisa"]["thinking_status"] == "is reviewing findings\u2026"
