"""SQLite-backed store for scheduled task records.

A scheduled task binds an agent to a prompt, a cron schedule, and a Slack
destination. The scheduler daemon reads rows where ``enabled=1`` and
``next_run_at <= now`` to decide which tasks to fire.

System tasks (``callable_ref`` set) are a second flavor of scheduled task:
the scheduler imports the dotted path and invokes it directly with the
stored ``payload`` and ``period_seconds`` cadence, no Claude session
involved. See :func:`ScheduledTaskStore.create_system_task` and
:mod:`router.dispatch.supervision` for the dispatch-supervision usage
that motivated them (#163).

Access methods accept an optional ``agent_name`` filter so callers can scope
reads and mutations to a single agent — this is how we prevent one agent from
touching another agent's schedule.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Marker stored in the NOT NULL ``schedule_cron`` column for system tasks
# (which use ``period_seconds`` instead of a cron expression). Empty string
# is unambiguous — cron expressions always have five non-empty fields.
SYSTEM_TASK_CRON_MARKER = ""

# Columns added after the initial schema. Tracked here so :meth:`_init_schema`
# can ALTER TABLE-add them on databases created by an older deployment
# without forcing a manual migration step.
_LATE_COLUMNS = (
    ("callable_ref", "TEXT"),
    ("payload", "TEXT"),
    ("period_seconds", "INTEGER"),
    ("one_shot", "INTEGER NOT NULL DEFAULT 0"),
)

# Maximum number of pending rows per agent across all insert paths.
MAX_PENDING_TASKS_PER_AGENT = 50


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
    # System-task fields (see module docstring). ``callable_ref`` is the
    # dotted path (``pkg.module:attr``) the scheduler imports; ``payload``
    # is the dict passed to it on each tick; ``period_seconds`` is the
    # polling cadence in lieu of a cron expression.
    callable_ref: str | None = None
    payload: dict | None = None
    period_seconds: int | None = None
    # one_shot=True means: delete the row after the first fire (used by wakeup verbs, #312).
    one_shot: bool = False

    @property
    def is_system_task(self) -> bool:
        return bool(self.callable_ref)

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
            "callable_ref": self.callable_ref,
            "payload": json.dumps(self.payload) if self.payload is not None else None,
            "period_seconds": self.period_seconds,
            "one_shot": 1 if self.one_shot else 0,
        }


def _row_value(row: sqlite3.Row, key: str, default=None):
    """Read a column safely.

    ``sqlite3.Row`` raises ``IndexError`` on unknown columns — undesirable
    here because we want callers that hand us older rows (no
    ``callable_ref`` column, mocks, fixtures) to silently default.
    """
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    payload_raw = _row_value(row, "payload")
    payload = json.loads(payload_raw) if isinstance(payload_raw, str) and payload_raw else None
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
        callable_ref=_row_value(row, "callable_ref"),
        payload=payload,
        period_seconds=_row_value(row, "period_seconds"),
        one_shot=bool(_row_value(row, "one_shot", 0)),
    )


class ScopeError(PermissionError):
    """Raised when an agent tries to read or mutate another agent's task."""


class ScheduledTaskStore:
    """SQLite-backed store for scheduled_tasks rows."""

    def __init__(self, db_path: str = "scheduled_tasks.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        # CREATE TABLE / CREATE INDEX for the *base* columns. The
        # system-task columns are added by the ALTER TABLE step below
        # so legacy DBs migrate cleanly on first open.
        self._conn.executescript(SCHEMA_PATH.read_text())
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(scheduled_tasks)")}
        for column, column_type in _LATE_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {column} {column_type}")
        # Now that callable_ref is guaranteed to exist, add its index.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_callable_ref ON scheduled_tasks(callable_ref)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def create(self, task: ScheduledTask) -> ScheduledTask:
        """Insert a new scheduled task record.

        Raises ``ValueError`` if the agent already has ``MAX_PENDING_TASKS_PER_AGENT``
        rows — prevents runaway wakeup scheduling from filling the table.
        """
        count = self._conn.execute(
            "SELECT COUNT(*) FROM scheduled_tasks WHERE agent_name = ?",
            (task.agent_name,),
        ).fetchone()[0]
        if count >= MAX_PENDING_TASKS_PER_AGENT:
            raise ValueError(
                f"Agent {task.agent_name!r} has reached the {MAX_PENDING_TASKS_PER_AGENT} pending task ceiling"
            )
        self._conn.execute(
            """
            INSERT INTO scheduled_tasks (
                task_id, agent_name, name, prompt, schedule_cron,
                destination, enabled, created_at, last_run_at, next_run_at,
                callable_ref, payload, period_seconds, one_shot
            ) VALUES (
                :task_id, :agent_name, :name, :prompt, :schedule_cron,
                :destination, :enabled, :created_at, :last_run_at, :next_run_at,
                :callable_ref, :payload, :period_seconds, :one_shot
            )
            """,
            task.to_row(),
        )
        self._conn.commit()
        return task

    def create_system_task(
        self,
        *,
        agent_name: str,
        name: str,
        callable_ref: str,
        payload: dict,
        period_seconds: int,
        destination: str | None = None,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> ScheduledTask:
        """Insert a system task — periodic Python callable instead of a Claude prompt.

        ``callable_ref`` is a dotted path of the form ``pkg.module:attr``
        which :mod:`router.scheduled_tasks.scheduler` imports and invokes
        on each tick. ``payload`` is the dict passed to the callable
        (stored as JSON). ``period_seconds`` is the polling cadence — the
        scheduler reschedules ``next_run_at`` to ``now + period_seconds``
        after every tick that doesn't deregister the task.
        """
        if period_seconds <= 0:
            raise ValueError(f"period_seconds must be > 0 (got {period_seconds})")
        if ":" not in callable_ref:
            raise ValueError(f"callable_ref must be 'pkg.module:attr' (got {callable_ref!r})")

        now = now or datetime.now(timezone.utc)
        task = ScheduledTask(
            task_id=task_id or str(uuid.uuid4()),
            agent_name=agent_name,
            name=name,
            # System tasks don't go through dispatch_fn — the prompt
            # column is unused but NOT NULL, so we satisfy the schema
            # with an empty string and let SYSTEM_TASK_CRON_MARKER mark
            # the row as "no cron, use period_seconds".
            prompt="",
            schedule_cron=SYSTEM_TASK_CRON_MARKER,
            destination=destination,
            enabled=True,
            created_at=now,
            next_run_at=now + timedelta(seconds=period_seconds),
            callable_ref=callable_ref,
            payload=payload,
            period_seconds=period_seconds,
        )
        return self.create(task)

    def create_one_shot_task(
        self,
        *,
        agent_name: str,
        next_run_at: datetime,
        payload: dict,
        name: str = "wakeup",
        destination: str | None = None,
        task_id: str | None = None,
    ) -> ScheduledTask:
        """Insert a one-shot wakeup task that deletes itself after firing (#312).

        ``payload`` must include ``channel_id`` and ``thread_ts`` captured from
        the calling agent's environment so the scheduler can re-inject them at
        fire time.
        """
        task = ScheduledTask(
            task_id=task_id or str(uuid.uuid4()),
            agent_name=agent_name,
            name=name,
            prompt="",
            schedule_cron=SYSTEM_TASK_CRON_MARKER,
            destination=destination,
            enabled=True,
            created_at=datetime.now(timezone.utc),
            next_run_at=next_run_at,
            one_shot=True,
            payload=payload,
        )
        return self.create(task)

    def update_payload(self, task_id: str, payload: dict) -> None:
        """Overwrite the ``payload`` JSON for a task in place."""
        self._conn.execute(
            "UPDATE scheduled_tasks SET payload = ? WHERE task_id = ?",
            (json.dumps(payload), task_id),
        )
        self._conn.commit()

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

    def list_by_callable_ref(self, callable_ref: str) -> list[ScheduledTask]:
        """List every task (enabled or not) registered under ``callable_ref``.

        Used by the startup janitor to enumerate active dispatch
        supervisions and decide which need to be re-registered or
        deregistered after a router restart (#159).
        """
        cursor = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE callable_ref = ? ORDER BY next_run_at ASC",
            (callable_ref,),
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
