"""Unit tests for router.session_manager — session lifecycle management.

These tests define the interface that router/session_manager.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

session_manager = pytest.importorskip("router.session_manager", reason="router.session_manager not yet implemented")

pytestmark = pytest.mark.unit


class TestSessionCreation:
    """Tests for creating new sessions."""

    def test_create_session_returns_session_id(self):
        """create_session() should return a unique session ID string."""
        session = session_manager.create_session(
            channel="C0001",
            thread_ts="1705700000.000100",
            agent_name="lisa",
        )
        assert isinstance(session["session_id"], str)
        assert len(session["session_id"]) > 0

    def test_create_session_stores_metadata(self):
        """Created session should store channel, thread_ts, and agent_name."""
        session = session_manager.create_session(
            channel="C0001",
            thread_ts="1705700000.000100",
            agent_name="lisa",
        )
        assert session["channel"] == "C0001"
        assert session["thread_ts"] == "1705700000.000100"
        assert session["agent_name"] == "lisa"

    def test_create_session_unique_ids(self):
        """Each call to create_session() should produce a unique session ID."""
        s1 = session_manager.create_session(channel="C0001", thread_ts="1.0", agent_name="lisa")
        s2 = session_manager.create_session(channel="C0001", thread_ts="2.0", agent_name="lisa")
        assert s1["session_id"] != s2["session_id"]


class TestTimeoutDetection:
    """Tests for session timeout detection."""

    def test_new_session_is_not_timed_out(self):
        """A freshly created session should not be timed out."""
        session = session_manager.create_session(
            channel="C0001",
            thread_ts="1705700000.000100",
            agent_name="lisa",
        )
        assert not session_manager.is_timed_out(session["session_id"])

    def test_timeout_detection_with_custom_threshold(self):
        """is_timed_out() should accept a custom timeout_seconds parameter."""
        session = session_manager.create_session(
            channel="C0001",
            thread_ts="1705700000.000100",
            agent_name="lisa",
        )
        # With a zero-second timeout, the session should immediately be timed out
        assert session_manager.is_timed_out(session["session_id"], timeout_seconds=0)


class TestSessionCleanup:
    """Tests for session cleanup."""

    def test_cleanup_removes_session(self):
        """cleanup_session() should remove the session so it can no longer be found."""
        session = session_manager.create_session(
            channel="C0001",
            thread_ts="1705700000.000100",
            agent_name="lisa",
        )
        session_manager.cleanup_session(session["session_id"])
        assert session_manager.get_session(session["session_id"]) is None

    def test_cleanup_nonexistent_session_no_error(self):
        """Cleaning up a nonexistent session should not raise an error."""
        session_manager.cleanup_session("nonexistent-session-id")
