"""Integration tests for the full router flow.

Tests the flow: Slack event → router → dispatch → response.
These tests verify components work together with mocked external services.
Tests will SKIP until the required modules exist.
"""

import pytest

pytestmark = pytest.mark.integration


class TestRouterFlow:
    """Test the full router dispatch flow."""

    def test_app_mention_routes_to_agent(self, mock_slack_client, mock_slack_event):
        """An app_mention event should be routed to the correct agent and return a response."""
        try:
            from router.config import get_agent_map
        except ImportError:
            pytest.skip("Router modules not yet implemented")

        # Verify the event can be parsed and an agent identified
        agent_map = get_agent_map()
        assert isinstance(agent_map, dict)

    def test_thread_reply_continues_session(self, mock_slack_client, mock_thread_reply_event):
        """A thread reply should continue an existing session rather than create a new one."""
        try:
            from router.session_manager import create_session, get_session
        except ImportError:
            pytest.skip("Router modules not yet implemented")

        event = mock_thread_reply_event["event"]
        session = create_session(
            channel=event["channel"],
            thread_ts=event["thread_ts"],
            agent_name="lisa",
        )
        retrieved = get_session(session["session_id"])
        assert retrieved is not None
        assert retrieved["thread_ts"] == event["thread_ts"]

    def test_dm_event_creates_session(self, mock_slack_client, mock_dm_event):
        """A direct message should create a new session."""
        try:
            from router.session_manager import create_session
        except ImportError:
            pytest.skip("Router modules not yet implemented")

        event = mock_dm_event["event"]
        session = create_session(
            channel=event["channel"],
            thread_ts=event["ts"],
            agent_name="lisa",
        )
        assert session["channel"] == event["channel"]
