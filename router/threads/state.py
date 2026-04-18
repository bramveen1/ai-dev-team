"""SQLite-backed store for per-thread active agent state.

A Slack thread can be handed off between multiple agents. This module
remembers which agent is "active" in a given (channel_id, thread_ts) so
that unmentioned follow-up messages are dispatched to the correct agent.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = "thread_state.db"


@dataclass
class ThreadState:
    """Active agent + last mention time for a single Slack thread."""

    channel_id: str
    thread_ts: str
    active_agent: str
    last_mention_at: datetime
    updated_at: datetime


class ThreadStateStore:
    """SQLite-backed store mapping (channel_id, thread_ts) -> active agent."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        # check_same_thread=False because the router's asyncio loop runs on
        # multiple OS threads via to_thread for SQLite operations.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        schema_sql = SCHEMA_PATH.read_text()
        with self._lock:
            self._conn.executescript(schema_sql)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, channel_id: str, thread_ts: str) -> ThreadState | None:
        """Return the thread's active agent record, or None if absent."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM thread_state WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    def get_active_agent(self, channel_id: str, thread_ts: str) -> str | None:
        """Convenience: return just the active agent name, or None."""
        state = self.get(channel_id, thread_ts)
        return state.active_agent if state else None

    def set_active_agent(
        self,
        channel_id: str,
        thread_ts: str,
        agent_name: str,
        mentioned: bool = False,
        now: datetime | None = None,
    ) -> ThreadState:
        """Record ``agent_name`` as the active agent for the thread.

        Args:
            channel_id: Slack channel ID.
            thread_ts: Slack thread parent timestamp.
            agent_name: Agent that is now active in the thread.
            mentioned: True if this update was triggered by an explicit
                mention. When True, ``last_mention_at`` is bumped; when False
                (e.g. an unmentioned follow-up dispatched to the active agent)
                only ``updated_at`` is bumped.
            now: Override the clock (useful in tests).

        Returns:
            The stored :class:`ThreadState`.
        """
        now = now or datetime.now(timezone.utc)
        existing = self.get(channel_id, thread_ts)
        last_mention_at = now if mentioned or existing is None else existing.last_mention_at

        row = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "active_agent": agent_name,
            "last_mention_at": last_mention_at.isoformat(),
            "updated_at": now.isoformat(),
        }

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO thread_state (
                    channel_id, thread_ts, active_agent, last_mention_at, updated_at
                ) VALUES (
                    :channel_id, :thread_ts, :active_agent, :last_mention_at, :updated_at
                )
                ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                    active_agent = excluded.active_agent,
                    last_mention_at = excluded.last_mention_at,
                    updated_at = excluded.updated_at
                """,
                row,
            )
            self._conn.commit()

        logger.info(
            "Thread state updated: channel=%s thread=%s agent=%s mentioned=%s",
            channel_id,
            thread_ts,
            agent_name,
            mentioned,
        )
        return ThreadState(
            channel_id=channel_id,
            thread_ts=thread_ts,
            active_agent=agent_name,
            last_mention_at=last_mention_at,
            updated_at=now,
        )

    def clear(self, channel_id: str, thread_ts: str) -> bool:
        """Remove the state row for a thread. Returns True if a row was removed."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM thread_state WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            )
            self._conn.commit()
        return cursor.rowcount > 0


def _row_to_state(row: sqlite3.Row) -> ThreadState:
    return ThreadState(
        channel_id=row["channel_id"],
        thread_ts=row["thread_ts"],
        active_agent=row["active_agent"],
        last_mention_at=datetime.fromisoformat(row["last_mention_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


_default_store: ThreadStateStore | None = None
_default_store_lock = threading.Lock()


def get_default_store(db_path: str | None = None) -> ThreadStateStore:
    """Return a process-wide :class:`ThreadStateStore` singleton.

    The router uses one shared store. Tests can pass their own ``db_path``
    to construct a fresh store or can monkeypatch this module.
    """
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = ThreadStateStore(db_path or DEFAULT_DB_PATH)
        return _default_store


def reset_default_store() -> None:
    """Clear the module-level singleton (used by tests)."""
    global _default_store
    with _default_store_lock:
        if _default_store is not None:
            _default_store.close()
        _default_store = None
