"""Unit tests for active thinking status — placeholder post + delete pattern.

Verifies that the router posts a placeholder message when an event arrives,
then deletes it and posts the final response as a fresh message.
"""

from unittest.mock import AsyncMock, patch

import pytest

from router.app import (
    DEFAULT_THINKING_STATUS,
    _handle_event,
    delete_status,
    post_status,
)

pytestmark = pytest.mark.unit

PLACEHOLDER_TS = "1705700050.000500"
FINAL_RESPONSE = "Here's my analysis of the auth module."


# ── Helper functions ────────────────────────────────────────────────


class TestPostStatus:
    """Tests for the post_status helper."""

    @pytest.mark.asyncio
    async def test_post_status_calls_chat_post_message(self, mock_slack_client):
        ts = await post_status(mock_slack_client, "C0001", "1.0", "Reviewing findings\u2026")
        mock_slack_client.chat_postMessage.assert_called_once_with(
            channel="C0001",
            thread_ts="1.0",
            text="Reviewing findings\u2026",
        )
        assert ts == "1705700000.000100"

    @pytest.mark.asyncio
    async def test_post_status_returns_message_ts(self, mock_slack_client):
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        ts = await post_status(mock_slack_client, "C0001", "1.0", "Thinking\u2026")
        assert ts == PLACEHOLDER_TS


class TestDeleteStatus:
    """Tests for the delete_status helper."""

    @pytest.mark.asyncio
    async def test_delete_status_calls_chat_delete(self, mock_slack_client):
        await delete_status(mock_slack_client, "C0001", PLACEHOLDER_TS)
        mock_slack_client.chat_delete.assert_called_once_with(
            channel="C0001",
            ts=PLACEHOLDER_TS,
        )

    @pytest.mark.asyncio
    async def test_delete_status_swallows_errors(self, mock_slack_client):
        mock_slack_client.chat_delete = AsyncMock(side_effect=Exception("rate limited"))
        await delete_status(mock_slack_client, "C0001", PLACEHOLDER_TS)


# ── Placeholder lifecycle in _handle_event ──────────────────────────


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


class TestHandleEventPlaceholder:
    """Tests for the placeholder post → delete → fresh response flow in _handle_event."""

    @pytest.mark.asyncio
    async def test_posts_placeholder_before_dispatch(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """A placeholder status message should be posted before the agent runs."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        # Placeholder posted with Lisa's configured thinking status
        mock_slack_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_slack_client.chat_postMessage.call_args[1]
        assert call_kwargs["text"] == "Reviewing findings\u2026"
        assert call_kwargs["channel"] == "C0001"

    @pytest.mark.asyncio
    async def test_deletes_placeholder_on_success(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """The placeholder should be deleted when the agent responds."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.chat_delete.assert_called_once_with(
            channel="C0001",
            ts=PLACEHOLDER_TS,
        )

    @pytest.mark.asyncio
    async def test_posts_fresh_response_via_say(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """The final response should be posted as a fresh message via say()."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        say.assert_called_once()
        assert say.call_args[1]["text"] == FINAL_RESPONSE

    @pytest.mark.asyncio
    async def test_no_chat_update_called(self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger):
        """chat_update should NOT be used — we delete + post fresh instead."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.chat_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_reactions_add_called(self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger):
        """The old \ud83d\udc40 reaction should NOT be used."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.reactions_add.assert_not_called()


class TestHandleEventPlaceholderErrors:
    """Tests for error handling with the placeholder pattern."""

    @pytest.mark.asyncio
    async def test_dispatch_error_deletes_placeholder(self, mock_slack_client, mock_session, mock_exit_trigger):
        """On dispatch failure, the placeholder should be deleted."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        with patch("router.app.dispatch", new_callable=AsyncMock, side_effect=RuntimeError("CLI crashed")):
            await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.chat_delete.assert_called_once_with(
            channel="C0001",
            ts=PLACEHOLDER_TS,
        )

    @pytest.mark.asyncio
    async def test_dispatch_error_posts_error_via_say(self, mock_slack_client, mock_session, mock_exit_trigger):
        """On dispatch failure, error message should be posted as a fresh message."""
        mock_slack_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": PLACEHOLDER_TS})
        say = AsyncMock()

        with patch("router.app.dispatch", new_callable=AsyncMock, side_effect=RuntimeError("CLI crashed")):
            await _handle_event(_make_event(), say, mock_slack_client)

        say.assert_called_once()
        assert "something went wrong" in say.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_placeholder_post_failure_falls_back_to_say(
        self, mock_slack_client, mock_dispatch, mock_session, mock_exit_trigger
    ):
        """If posting the placeholder fails, fall back to say() for the response."""
        mock_slack_client.chat_postMessage = AsyncMock(side_effect=Exception("rate limited"))
        say = AsyncMock()

        await _handle_event(_make_event(), say, mock_slack_client)

        # chat_delete should not be called since we have no placeholder ts
        mock_slack_client.chat_delete.assert_not_called()
        # Instead, fall back to say()
        say.assert_called_once()
        assert say.call_args[1]["text"] == FINAL_RESPONSE

    @pytest.mark.asyncio
    async def test_placeholder_post_failure_and_dispatch_error_falls_back_to_say(
        self, mock_slack_client, mock_session, mock_exit_trigger
    ):
        """If both placeholder and dispatch fail, error goes through say()."""
        mock_slack_client.chat_postMessage = AsyncMock(side_effect=Exception("rate limited"))
        say = AsyncMock()

        with patch("router.app.dispatch", new_callable=AsyncMock, side_effect=RuntimeError("CLI crashed")):
            await _handle_event(_make_event(), say, mock_slack_client)

        mock_slack_client.chat_delete.assert_not_called()
        say.assert_called_once()
        assert "something went wrong" in say.call_args[1]["text"].lower()


class TestDefaultThinkingStatus:
    """Tests for configurable thinking status text."""

    def test_default_thinking_status_exists(self):
        """DEFAULT_THINKING_STATUS should be defined as a fallback."""
        assert DEFAULT_THINKING_STATUS == "Thinking\u2026"

    def test_lisa_has_custom_thinking_status(self):
        """Lisa's agent config should have a custom thinking_status."""
        from router.config import get_agent_map

        agent_map = get_agent_map()
        assert agent_map["lisa"]["thinking_status"] == "Reviewing findings\u2026"
