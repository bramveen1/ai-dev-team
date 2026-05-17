CREATE TABLE IF NOT EXISTS scheduled_tasks (
  task_id TEXT PRIMARY KEY,
  agent_name TEXT NOT NULL,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  schedule_cron TEXT NOT NULL,
  destination TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL,
  last_run_at TIMESTAMP,
  next_run_at TIMESTAMP NOT NULL,
  -- System-task columns (added for dispatch supervision, #163). When
  -- callable_ref is set the scheduler imports and invokes the dotted
  -- path directly instead of going through dispatch_fn; period_seconds
  -- and payload drive the polling cadence and the callable's input.
  callable_ref TEXT,
  payload TEXT,
  period_seconds INTEGER
);

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_agent ON scheduled_tasks(agent_name);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next_run ON scheduled_tasks(next_run_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled ON scheduled_tasks(enabled);
-- Indexes on the system-task columns are created in Python after the
-- migration step adds the columns to legacy databases — defining them
-- here breaks first-open on a pre-#163 DB because the column doesn't
-- exist yet at the time `executescript` runs the CREATE INDEX.
