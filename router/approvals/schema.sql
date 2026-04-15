CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY,
  agent_name TEXT NOT NULL,
  capability_type TEXT NOT NULL,
  capability_instance TEXT NOT NULL,
  action_verb TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  slack_channel TEXT NOT NULL,
  slack_message_ts TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL,
  resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_channel_ts ON drafts(slack_channel, slack_message_ts);
