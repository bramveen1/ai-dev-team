"""Router session manager — tracks active agent sessions per thread.

Sessions are keyed by a unique session ID and store metadata about
the active conversation (channel, thread, agent, timestamps).
"""

from __future__ import annotations

import logging
import time
import uuid

logger = logging.getLogger(__name__)

# In-memory session store. Keyed by session_id.
_sessions: dict[str, dict] = {}

# Default timeout in seconds (10 minutes)
DEFAULT_TIMEOUT_SECONDS = 600


def create_session(channel: str, thread_ts: str, agent_name: str) -> dict:
    """Create a new session for a channel/thread/agent combination.

    Args:
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp.
        agent_name: Name of the agent handling this session.

    Returns:
        A session dict with keys: session_id, channel, thread_ts,
        agent_name, created_at, last_activity.
    """
    session_id = str(uuid.uuid4())
    now = time.time()

    session = {
        "session_id": session_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "agent_name": agent_name,
        "created_at": now,
        "last_activity": now,
        "thread_history": [],
    }

    _sessions[session_id] = session
    logger.info("Created session %s for agent=%s channel=%s thread=%s", session_id, agent_name, channel, thread_ts)
    return session


def get_session(session_id: str) -> dict | None:
    """Retrieve a session by its ID.

    Returns the session dict, or None if not found.
    """
    return _sessions.get(session_id)


def update_activity(session_id: str) -> None:
    """Update the last_activity timestamp for a session.

    No-op if the session does not exist.
    """
    session = _sessions.get(session_id)
    if session is not None:
        session["last_activity"] = time.time()
        logger.debug("Updated activity for session %s", session_id)


def is_timed_out(session_id: str, timeout_seconds: int | None = None) -> bool:
    """Check whether a session has exceeded the timeout threshold.

    Args:
        session_id: The session to check.
        timeout_seconds: Custom timeout in seconds. Uses DEFAULT_TIMEOUT_SECONDS if None.

    Returns:
        True if the session is timed out or does not exist, False otherwise.
    """
    session = _sessions.get(session_id)
    if session is None:
        return True

    if timeout_seconds is None:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    elapsed = time.time() - session["last_activity"]
    return elapsed >= timeout_seconds


def cleanup_session(session_id: str) -> None:
    """Remove a session from the store.

    No-op if the session does not exist.
    """
    removed = _sessions.pop(session_id, None)
    if removed:
        logger.info("Cleaned up session %s", session_id)


def find_session_by_thread(channel: str, thread_ts: str) -> dict | None:
    """Find an active session for a given channel and thread.

    Returns the most recently active session dict, or None if not found.
    """
    matches = [
        s
        for s in _sessions.values()
        if s["channel"] == channel and s["thread_ts"] == thread_ts and not is_timed_out(s["session_id"])
    ]
    if not matches:
        return None
    # Return the most recently active session
    return max(matches, key=lambda s: s["last_activity"])


def add_to_thread_history(session_id: str, message: dict) -> None:
    """Append a message to the session's thread history.

    Args:
        session_id: The session to update.
        message: A dict with at least 'user' and 'text' keys.
    """
    session = _sessions.get(session_id)
    if session is not None:
        session["thread_history"].append(message)
        msg_count = len(session["thread_history"])
        logger.debug("Added message to thread history for session %s (now %d msgs)", session_id, msg_count)


def get_thread_history(session_id: str) -> list[dict]:
    """Return the thread history for a session.

    Args:
        session_id: The session to query.

    Returns:
        List of message dicts, or empty list if session not found.
    """
    session = _sessions.get(session_id)
    if session is not None:
        return list(session["thread_history"])
    return []


def get_active_sessions() -> list[dict]:
    """Return a list of all active (non-timed-out) sessions."""
    return [s for s in _sessions.values() if not is_timed_out(s["session_id"])]


def pop_timed_out_sessions(timeout_seconds: int | None = None) -> list[dict]:
    """Remove all timed-out sessions and return their data.

    Returns a list of session dicts that were removed, so the caller
    can perform cleanup actions (e.g. posting summaries) with the
    session metadata.
    """
    timed_out = [sid for sid in _sessions if is_timed_out(sid, timeout_seconds)]
    removed = []
    for sid in timed_out:
        session = _sessions.pop(sid, None)
        if session:
            logger.info("Session %s timed out, cleaning up", sid)
            removed.append(session)
    return removed
