CREATE TABLE IF NOT EXISTS thread_state (
  channel_id       TEXT NOT NULL,
  thread_ts        TEXT NOT NULL,
  active_agent     TEXT NOT NULL,
  last_mention_at  TIMESTAMP NOT NULL,
  updated_at       TIMESTAMP NOT NULL,
  PRIMARY KEY (channel_id, thread_ts)
);

CREATE INDEX IF NOT EXISTS idx_thread_state_updated
  ON thread_state(updated_at);
