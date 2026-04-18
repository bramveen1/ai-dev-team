"""SQLite-backed store for scheduled task records.

A scheduled task binds an agent to a prompt, a cron schedule, and a Slack
destination. The scheduler daemon reads rows where ``enabled=1`` and
``next_run_at <= now`` to decide which tasks to fire.

Access methods accept an optional ``agent_name`` filter so callers can scope
reads and mutations to a single agent — this is how we prevent one agent from
touching another agent's schedule.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@dataclass
class ScheduledTask:
    """A single scheduled task record."""

    task_id: str
    agent_name: str
    name: str
    prompt: str
    schedule_cron: str
    next_run_at: datetime
    destination: str | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_run_at: datetime | None = None

    def to_row(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "name": self.name,
            "prompt": self.prompt,
            "schedule_cron": self.schedule_cron,
            "destination": self.destination,
            "enabled": 1 if self.enabled else 0,
            "created_at": self.created_at.isoformat(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat(),
        }


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    return ScheduledTask(
        task_id=row["task_id"],
        agent_name=row["agent_name"],
        name=row["name"],
        prompt=row["prompt"],
        schedule_cron=row["schedule_cron"],
        destination=row["destination"],
        enabled=bool(row["enabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_run_at=datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None,
        next_run_at=datetime.fromisoformat(row["next_run_at"]),
    )


class ScopeError(PermissionError):
    """Raised when an agent tries to read or mutate another agent's task."""


class ScheduledTaskStore:
    """SQLite-backed store for scheduled_tasks rows."""

    def __init__(self, db_path: str = "scheduled_tasks.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_PATH.read_text())

    def close(self) -> None:
        self._conn.close()

    def create(self, task: ScheduledTask) -> ScheduledTask:
        """Insert a new scheduled task record."""
        self._conn.execute(
            """
            INSERT INTO scheduled_tasks (
                task_id, agent_name, name, prompt, schedule_cron,
                destination, enabled, created_at, last_run_at, next_run_at
            ) VALUES (
                :task_id, :agent_name, :name, :prompt, :schedule_cron,
                :destination, :enabled, :created_at, :last_run_at, :next_run_at
            )
            """,
            task.to_row(),
        )
        self._conn.commit()
        return task

    def get(self, task_id: str, agent_name: str | None = None) -> ScheduledTask | None:
        """Fetch a task by ID. If ``agent_name`` is given, enforce ownership scope."""
        cursor = self._conn.execute("SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        task = _row_to_task(row)
        if agent_name is not None and task.agent_name != agent_name:
            raise ScopeError(f"Agent {agent_name!r} cannot access task owned by {task.agent_name!r}")
        return task

    def list_for_agent(self, agent_name: str, enabled_only: bool = False) -> list[ScheduledTask]:
        """List all tasks owned by ``agent_name``."""
        if enabled_only:
            cursor = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE agent_name = ? AND enabled = 1 ORDER BY next_run_at ASC",
                (agent_name,),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE agent_name = ? ORDER BY next_run_at ASC",
                (agent_name,),
            )
        return [_row_to_task(row) for row in cursor.fetchall()]

    def list_due(self, now: datetime) -> list[ScheduledTask]:
        """List enabled tasks whose ``next_run_at`` is at or before ``now``."""
        cursor = self._conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE enabled = 1 AND next_run_at <= ?
            ORDER BY next_run_at ASC
            """,
            (now.isoformat(),),
        )
        return [_row_to_task(row) for row in cursor.fetchall()]

    def set_enabled(self, task_id: str, enabled: bool, agent_name: str | None = None) -> ScheduledTask:
        """Pause or resume a task. Raises ScopeError if agent doesn't own it."""
        task = self.get(task_id, agent_name=agent_name)
        if task is None:
            raise KeyError(f"Task {task_id} not found")

        self._conn.execute(
            "UPDATE scheduled_tasks SET enabled = ? WHERE task_id = ?",
            (1 if enabled else 0, task_id),
        )
        self._conn.commit()
        task.enabled = enabled
        return task

    def update_run_times(self, task_id: str, last_run_at: datetime, next_run_at: datetime) -> ScheduledTask:
        """Record that a task ran at ``last_run_at`` and set the new ``next_run_at``."""
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"Task {task_id} not found")

        self._conn.execute(
            "UPDATE scheduled_tasks SET last_run_at = ?, next_run_at = ? WHERE task_id = ?",
            (last_run_at.isoformat(), next_run_at.isoformat(), task_id),
        )
        self._conn.commit()
        task.last_run_at = last_run_at
        task.next_run_at = next_run_at
        return task

    def delete(self, task_id: str, agent_name: str | None = None) -> bool:
        """Delete a task. If ``agent_name`` is given, only delete when the agent owns the row.

        Returns True if a row was deleted. Returns False when the row does not exist or
        when ``agent_name`` is set and the row belongs to a different agent — this keeps
        the scoped interface quiet (the caller treats "can't touch it" the same as "not there").
        """
        if agent_name is not None:
            existing = self.get(task_id)
            if existing is None or existing.agent_name != agent_name:
                return False

        cursor = self._conn.execute("DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,))
        self._conn.commit()
        return cursor.rowcount > 0
