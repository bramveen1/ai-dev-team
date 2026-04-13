"""Unit tests for router.dispatcher — Claude CLI dispatch and error handling.

Tests mock ``_run_in_container`` so no Docker daemon is required.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from router.dispatcher import (
    DEFAULT_TIMEOUT_SECONDS,
    DispatchError,
    DispatchTimeoutError,
    dispatch,
)

pytestmark = pytest.mark.unit

# Standard successful CLI JSON response
_CLI_RESULT_TEXT = "I'm Lisa, the project manager. How can I help you today?"
_MOCK_CLI_STDOUT = json.dumps(
    {
        "result": _CLI_RESULT_TEXT,
        "session_id": "test-session-00000000",
        "total_cost_usd": 0.012,
        "usage": {"input_tokens": 120, "output_tokens": 58},
    }
)


@pytest.fixture()
def mock_container():
    """Mock _run_in_container to return a successful CLI response."""
    with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
        mock.return_value = (_MOCK_CLI_STDOUT, "", 0)
        yield mock


# ── Routing ──────────────────────────────────────────────────────────


class TestDispatchRouting:
    """Tests for routing messages to the correct agent."""

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_lisa(self, mock_slack_client, mock_container):
        """Mentioning 'lisa' should route to the Lisa agent."""
        result = await dispatch(
            agent_name="lisa",
            message="Please review the auth module",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        assert result is not None
        assert result.get("agent") == "lisa"

    @pytest.mark.asyncio
    async def test_dispatch_returns_response(self, mock_slack_client, mock_container):
        """Dispatch should return a response dict with expected keys."""
        result = await dispatch(
            agent_name="lisa",
            message="Fix the bug in auth.py",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        assert result["status"] == "ok"
        assert result["response"] == _CLI_RESULT_TEXT

    @pytest.mark.asyncio
    async def test_dispatch_invokes_container_with_correct_command(self, mock_slack_client, mock_container):
        """The docker exec command should include the right CLI flags."""
        await dispatch(
            agent_name="lisa",
            message="Hello Lisa",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        mock_container.assert_called_once()
        container, cli_cmd, _timeout = mock_container.call_args[0]
        assert container == "lisa"
        assert cli_cmd[0] == "claude"
        assert "-p" in cli_cmd
        assert "Hello Lisa" in cli_cmd
        assert "--bare" in cli_cmd
        assert "--output-format" in cli_cmd and "json" in cli_cmd
        assert "--append-system-prompt-file" in cli_cmd
        assert "/agent/role.md" in cli_cmd
        assert "--no-session-persistence" in cli_cmd
        assert "--max-turns" in cli_cmd


# ── Error handling ───────────────────────────────────────────────────


class TestDispatchErrorHandling:
    """Tests for error handling in dispatch."""

    @pytest.mark.asyncio
    async def test_unknown_agent_raises_error(self, mock_slack_client):
        """Dispatching to an unknown agent should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown agent"):
            await dispatch(
                agent_name="nonexistent_agent",
                message="Hello",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )

    @pytest.mark.asyncio
    async def test_empty_message_raises_error(self, mock_slack_client):
        """Dispatching with an empty message should raise ValueError."""
        with pytest.raises(ValueError):
            await dispatch(
                agent_name="lisa",
                message="",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )

    @pytest.mark.asyncio
    async def test_whitespace_message_raises_error(self, mock_slack_client):
        """Whitespace-only messages should raise ValueError."""
        with pytest.raises(ValueError):
            await dispatch(
                agent_name="lisa",
                message="   ",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )

    @pytest.mark.asyncio
    async def test_cli_nonzero_exit_raises_error(self, mock_slack_client):
        """Non-zero CLI exit code should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "Error: invalid API key", 1)
            with pytest.raises(DispatchError, match="exited with code 1"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_empty_stdout_raises_error(self, mock_slack_client):
        """Empty CLI stdout should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "", 0)
            with pytest.raises(DispatchError, match="empty response"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_invalid_json_raises_error(self, mock_slack_client):
        """Non-JSON CLI output should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("this is not json", "", 0)
            with pytest.raises(DispatchError, match="invalid JSON"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_empty_result_field_raises_error(self, mock_slack_client):
        """JSON with an empty 'result' field should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = (json.dumps({"result": "", "session_id": "x"}), "", 0)
            with pytest.raises(DispatchError, match="empty result"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_missing_result_field_raises_error(self, mock_slack_client):
        """JSON without a 'result' key should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = (json.dumps({"session_id": "x"}), "", 0)
            with pytest.raises(DispatchError, match="empty result"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )


# ── Timeout handling ─────────────────────────────────────────────────


class TestDispatchTimeout:
    """Tests for dispatch timeout handling."""

    @pytest.mark.asyncio
    async def test_dispatch_respects_custom_timeout(self, mock_slack_client, mock_container):
        """Custom timeout should be forwarded to _run_in_container."""
        result = await dispatch(
            agent_name="lisa",
            message="Quick task",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
            timeout=30,
        )
        assert result is not None
        _, _, timeout_used = mock_container.call_args[0]
        assert timeout_used == 30

    @pytest.mark.asyncio
    async def test_dispatch_uses_default_timeout(self, mock_slack_client, mock_container):
        """Omitting timeout should use DEFAULT_TIMEOUT_SECONDS."""
        await dispatch(
            agent_name="lisa",
            message="Hello",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        _, _, timeout_used = mock_container.call_args[0]
        assert timeout_used == DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_cli_timeout_raises_dispatch_timeout_error(self, mock_slack_client):
        """DispatchTimeoutError from _run_in_container should propagate."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.side_effect = DispatchTimeoutError("timed out after 30s")
            with pytest.raises(DispatchTimeoutError):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )
