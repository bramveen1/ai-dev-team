"""Unit tests for router.dispatcher — dispatch routing logic and error handling.

These tests define the interface that router/dispatcher.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

dispatcher = pytest.importorskip("router.dispatcher", reason="router.dispatcher not yet implemented")

pytestmark = pytest.mark.unit


class TestDispatchRouting:
    """Tests for routing messages to the correct agent."""

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_lisa(self, mock_slack_client):
        """Mentioning 'lisa' should route to the Lisa agent."""
        result = await dispatcher.dispatch(
            agent_name="lisa",
            message="Please review the auth module",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        assert result is not None
        assert result.get("agent") == "lisa"

    @pytest.mark.asyncio
    async def test_dispatch_returns_response(self, mock_slack_client):
        """Dispatch should return a response dict with 'status' and 'response' keys."""
        result = await dispatcher.dispatch(
            agent_name="lisa",
            message="Fix the bug in auth.py",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        assert "status" in result
        assert "response" in result


class TestDispatchErrorHandling:
    """Tests for error handling in dispatch."""

    @pytest.mark.asyncio
    async def test_unknown_agent_raises_error(self, mock_slack_client):
        """Dispatching to an unknown agent should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown agent"):
            await dispatcher.dispatch(
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
            await dispatcher.dispatch(
                agent_name="lisa",
                message="",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )


class TestDispatchTimeout:
    """Tests for dispatch timeout handling."""

    @pytest.mark.asyncio
    async def test_dispatch_respects_timeout(self, mock_slack_client):
        """Dispatch should accept a timeout parameter."""
        result = await dispatcher.dispatch(
            agent_name="lisa",
            message="Quick task",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
            timeout=30,
        )
        assert result is not None
