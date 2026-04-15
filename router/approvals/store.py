"""SQLite-backed draft state store for the approval flow.

Provides CRUD operations for draft records that track approval
requests posted to Slack. Each draft has a lifecycle:
pending -> approved | discarded | expired.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

VALID_STATUSES = {"pending", "approved", "discarded", "expired", "cleaned_up"}
VALID_TRANSITIONS = {
    "pending": {"approved", "discarded", "expired"},
    "expired": {"cleaned_up"},
}


@dataclass
class Draft:
    """A single draft awaiting approval."""

    draft_id: str
    agent_name: str
    capability_type: str
    capability_instance: str
    action_verb: str
    payload: dict[str, Any]
    slack_channel: str
    slack_message_ts: str
    draft_type: str = "direct"  # "direct" (agent executes on approval) or "native" (user acts in external app)
    status: str = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    reminded_at: datetime | None = None
    expires_at: datetime | None = None

    def to_row(self) -> dict[str, Any]:
        """Convert to a dict suitable for SQLite insertion."""
        return {
            "draft_id": self.draft_id,
            "agent_name": self.agent_name,
            "capability_type": self.capability_type,
            "capability_instance": self.capability_instance,
            "action_verb": self.action_verb,
            "payload_json": json.dumps(self.payload),
            "slack_channel": self.slack_channel,
            "slack_message_ts": self.slack_message_ts,
            "draft_type": self.draft_type,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "reminded_at": self.reminded_at.isoformat() if self.reminded_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def _row_to_draft(row: sqlite3.Row) -> Draft:
    """Convert a SQLite row to a Draft dataclass."""
    return Draft(
        draft_id=row["draft_id"],
        agent_name=row["agent_name"],
        capability_type=row["capability_type"],
        capability_instance=row["capability_instance"],
        action_verb=row["action_verb"],
        payload=json.loads(row["payload_json"]),
        slack_channel=row["slack_channel"],
        slack_message_ts=row["slack_message_ts"],
        draft_type=row["draft_type"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
        reminded_at=datetime.fromisoformat(row["reminded_at"]) if row["reminded_at"] else None,
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
    )


class DraftStore:
    """SQLite-backed store for draft approval records."""

    def __init__(self, db_path: str = "drafts.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the drafts table if it doesn't exist."""
        schema_sql = SCHEMA_PATH.read_text()
        self._conn.executescript(schema_sql)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def create(self, draft: Draft) -> Draft:
        """Insert a new draft record."""
        row = draft.to_row()
        self._conn.execute(
            """
            INSERT INTO drafts (
                draft_id, agent_name, capability_type, capability_instance,
                action_verb, payload_json, slack_channel, slack_message_ts,
                draft_type, status, created_at, resolved_at, reminded_at, expires_at
            ) VALUES (
                :draft_id, :agent_name, :capability_type, :capability_instance,
                :action_verb, :payload_json, :slack_channel, :slack_message_ts,
                :draft_type, :status, :created_at, :resolved_at, :reminded_at, :expires_at
            )
            """,
            row,
        )
        self._conn.commit()
        return draft

    def get(self, draft_id: str) -> Draft | None:
        """Fetch a draft by its ID. Returns None if not found."""
        cursor = self._conn.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_draft(row)

    def get_by_channel_ts(self, channel: str, message_ts: str) -> Draft | None:
        """Fetch a draft by its Slack channel and message timestamp."""
        cursor = self._conn.execute(
            "SELECT * FROM drafts WHERE slack_channel = ? AND slack_message_ts = ?",
            (channel, message_ts),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_draft(row)

    def list_by_status(self, status: str) -> list[Draft]:
        """List all drafts with a given status."""
        cursor = self._conn.execute(
            "SELECT * FROM drafts WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return [_row_to_draft(row) for row in cursor.fetchall()]

    def transition(self, draft_id: str, new_status: str) -> Draft:
        """Transition a draft to a new status.

        Raises ValueError if the transition is not allowed.
        Raises KeyError if the draft is not found.
        """
        draft = self.get(draft_id)
        if draft is None:
            raise KeyError(f"Draft {draft_id} not found")

        allowed = VALID_TRANSITIONS.get(draft.status, set())
        if new_status not in allowed:
            raise ValueError(f"Cannot transition from '{draft.status}' to '{new_status}'")

        now = datetime.now(timezone.utc)
        self._conn.execute(
            "UPDATE drafts SET status = ?, resolved_at = ? WHERE draft_id = ?",
            (new_status, now.isoformat(), draft_id),
        )
        self._conn.commit()

        draft.status = new_status
        draft.resolved_at = now
        return draft

    def mark_reminded(self, draft_id: str, reminded_at: datetime) -> Draft:
        """Record that a reminder was sent for this draft.

        Raises KeyError if the draft is not found.
        """
        draft = self.get(draft_id)
        if draft is None:
            raise KeyError(f"Draft {draft_id} not found")

        self._conn.execute(
            "UPDATE drafts SET reminded_at = ? WHERE draft_id = ?",
            (reminded_at.isoformat(), draft_id),
        )
        self._conn.commit()
        draft.reminded_at = reminded_at
        return draft

    def list_pending_needing_reminder(self, reminder_threshold: datetime) -> list[Draft]:
        """List pending drafts that need a reminder (created before threshold, not yet reminded)."""
        cursor = self._conn.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'pending'
              AND reminded_at IS NULL
              AND created_at <= ?
            ORDER BY created_at ASC
            """,
            (reminder_threshold.isoformat(),),
        )
        return [_row_to_draft(row) for row in cursor.fetchall()]

    def list_pending_expired(self, expiry_threshold: datetime) -> list[Draft]:
        """List pending drafts that have passed their expiration time."""
        cursor = self._conn.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'pending'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY created_at ASC
            """,
            (expiry_threshold.isoformat(),),
        )
        return [_row_to_draft(row) for row in cursor.fetchall()]

    def list_expired_needing_cleanup(self, cleanup_threshold: datetime) -> list[Draft]:
        """List expired native-app drafts ready for external resource cleanup."""
        cursor = self._conn.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'expired'
              AND draft_type = 'native'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY created_at ASC
            """,
            (cleanup_threshold.isoformat(),),
        )
        return [_row_to_draft(row) for row in cursor.fetchall()]

    def delete(self, draft_id: str) -> bool:
        """Delete a draft record. Returns True if a row was deleted."""
        cursor = self._conn.execute("DELETE FROM drafts WHERE draft_id = ?", (draft_id,))
        self._conn.commit()
        return cursor.rowcount > 0
