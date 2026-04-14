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


class TestFindSessionByThread:
    """Tests for finding sessions by channel and thread."""

    def test_find_existing_session(self):
        """find_session_by_thread() should return a session matching channel and thread_ts."""
        session = session_manager.create_session(
            channel="C_FIND",
            thread_ts="1705700000.000200",
            agent_name="lisa",
        )
        found = session_manager.find_session_by_thread("C_FIND", "1705700000.000200")
        assert found is not None
        assert found["session_id"] == session["session_id"]

    def test_find_returns_none_for_unknown_thread(self):
        """find_session_by_thread() should return None when no session matches."""
        found = session_manager.find_session_by_thread("C_NONE", "9999999999.000000")
        assert found is None

    def test_find_returns_none_for_timed_out_session(self):
        """find_session_by_thread() should not return timed-out sessions."""
        session_manager.create_session(
            channel="C_TIMEOUT",
            thread_ts="1705700000.000300",
            agent_name="lisa",
        )
        # With timeout_seconds=0 the session is immediately stale, but
        # find_session_by_thread uses the default timeout — force it by
        # manipulating last_activity
        sid = session_manager.find_session_by_thread("C_TIMEOUT", "1705700000.000300")
        assert sid is not None  # sanity: it exists before timeout
        # Manually expire it
        sid["last_activity"] = 0
        found = session_manager.find_session_by_thread("C_TIMEOUT", "1705700000.000300")
        assert found is None


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


class TestUpdateActivity:
    """Tests for update_activity."""

    def test_update_activity_changes_timestamp(self):
        """update_activity should update the last_activity timestamp."""
        import time

        session = session_manager.create_session(channel="C_UPD", thread_ts="1.0", agent_name="lisa")
        original_ts = session["last_activity"]
        time.sleep(0.01)
        session_manager.update_activity(session["session_id"])
        updated = session_manager.get_session(session["session_id"])
        assert updated["last_activity"] >= original_ts

    def test_update_activity_nonexistent_session(self):
        """update_activity on a nonexistent session should be a no-op."""
        session_manager.update_activity("nonexistent-id")  # Should not raise


class TestGetSession:
    """Tests for get_session."""

    def test_get_existing_session(self):
        """Should return the session dict for a valid ID."""
        session = session_manager.create_session(channel="C_GET", thread_ts="1.0", agent_name="lisa")
        found = session_manager.get_session(session["session_id"])
        assert found is not None
        assert found["session_id"] == session["session_id"]

    def test_get_nonexistent_session(self):
        """Should return None for unknown session ID."""
        assert session_manager.get_session("nonexistent") is None


class TestIsTimedOut:
    """Additional tests for is_timed_out edge cases."""

    def test_nonexistent_session_is_timed_out(self):
        """is_timed_out should return True for nonexistent sessions."""
        assert session_manager.is_timed_out("nonexistent") is True

    def test_uses_default_timeout(self):
        """is_timed_out without timeout_seconds should use DEFAULT_TIMEOUT_SECONDS."""
        session = session_manager.create_session(channel="C_DEF", thread_ts="1.0", agent_name="lisa")
        # Fresh session should not be timed out with default timeout
        assert session_manager.is_timed_out(session["session_id"]) is False


class TestAddToThreadHistory:
    """Tests for add_to_thread_history."""

    def test_adds_message_to_history(self):
        """Should append a message to session's thread history."""
        session = session_manager.create_session(channel="C_THR", thread_ts="1.0", agent_name="lisa")
        session_manager.add_to_thread_history(session["session_id"], {"user": "U001", "text": "hello"})
        history = session_manager.get_thread_history(session["session_id"])
        assert len(history) == 1
        assert history[0]["text"] == "hello"

    def test_add_to_nonexistent_session(self):
        """Adding to a nonexistent session should be a no-op."""
        session_manager.add_to_thread_history("nonexistent", {"user": "U001", "text": "hello"})


class TestGetThreadHistory:
    """Tests for get_thread_history."""

    def test_returns_copy(self):
        """Should return a copy, not the original list."""
        session = session_manager.create_session(channel="C_HIST", thread_ts="1.0", agent_name="lisa")
        session_manager.add_to_thread_history(session["session_id"], {"user": "U001", "text": "hello"})
        hist1 = session_manager.get_thread_history(session["session_id"])
        hist2 = session_manager.get_thread_history(session["session_id"])
        assert hist1 is not hist2

    def test_empty_for_nonexistent_session(self):
        """Should return empty list for nonexistent session."""
        assert session_manager.get_thread_history("nonexistent") == []


class TestGetActiveSessions:
    """Tests for get_active_sessions."""

    def test_returns_active_sessions(self):
        """Should return sessions that are not timed out."""
        session = session_manager.create_session(channel="C_ACTIVE", thread_ts="1.0", agent_name="lisa")
        active = session_manager.get_active_sessions()
        ids = [s["session_id"] for s in active]
        assert session["session_id"] in ids


class TestPopTimedOutSessions:
    """Tests for pop_timed_out_sessions."""

    def test_pops_expired_sessions(self):
        """Should remove and return timed-out sessions."""
        session = session_manager.create_session(channel="C_POP", thread_ts="1.0", agent_name="lisa")
        # Force timeout by setting last_activity to 0
        session_manager._sessions[session["session_id"]]["last_activity"] = 0
        removed = session_manager.pop_timed_out_sessions(timeout_seconds=1)
        ids = [s["session_id"] for s in removed]
        assert session["session_id"] in ids
        # Should be removed from store
        assert session_manager.get_session(session["session_id"]) is None

    def test_does_not_pop_active_sessions(self):
        """Active sessions should not be popped."""
        session = session_manager.create_session(channel="C_NOPOP", thread_ts="1.0", agent_name="lisa")
        removed = session_manager.pop_timed_out_sessions(timeout_seconds=9999)
        ids = [s["session_id"] for s in removed]
        assert session["session_id"] not in ids
