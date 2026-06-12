# Headless dispatch (Claude Code subprocess)

You can dispatch a fresh, headless Claude Code session to work on a
GitHub issue end-to-end. The dispatched session runs in its own
workspace under `/var/lib/dispatch/`, opens a branch, makes the
change, pushes, and opens a PR. The result comes back into the Slack
thread.

## Verbs

Call the handler from Bash:

```bash
python /config/packs/dispatch/handler.py <verb>
```

| Verb                          | Purpose                                                  |
| ----------------------------- | -------------------------------------------------------- |
| `dispatch_health`             | smoke probe — cli, workspace, sonnet round-trip          |
| `dispatch_issue`              | spawn a `claude -p` against a GitHub issue URL           |
| `dispatch_status`             | pool snapshot: running and queued dispatch IDs           |
| `dispatch_cancel`             | SIGTERM the process group, tear down workspace           |
| `dispatch_draft`              | create an approval card via the router's internal API    |
| `dispatch_list_pending_drafts`| list your outstanding drafts (recovery path)             |

## `dispatch_issue` — approval gating (D-7)

When `dispatch_issue` returns `{"status": "approval_required", ...}`,
a human must approve before the work starts. Do **not** spawn the
dispatch yourself. Instead, call `dispatch_draft` to create the
approval card:

```bash
python /config/packs/dispatch/handler.py dispatch_draft \
  --issue-url https://github.com/bramveen1/ai-dev-team/issues/42 \
  --model sonnet \
  --persona dev \
  --title "Fix the foo regression on bar"
```

On success the verb returns:

```json
{
  "status": "draft_created",
  "draft_id": "a1b2c3d4e5f6...",
  "card_ts": "1234567890.123456",
  "gate_reason": "always"
}
```

Report `draft_id` and `gate_reason` to the user and wait for them to
click the Approve button in Slack. You do **not** need to emit any
fenced block — the approval card is already posted by the router.

`gate_reason` values and what they mean:

| `gate_reason`         | Why the gate fired                                          |
| --------------------- | ----------------------------------------------------------- |
| `always`              | Pilot mode — every dispatch needs approval                  |
| `destructive_keyword` | `model=opus` and issue text contains a destructive keyword  |
| `cost_threshold`      | 5h window cost ≥ `DISPATCH_APPROVAL_COST_USD` (default $15) |
| `manual`              | No gate fired; you explicitly requested an approval card    |

## `dispatch_draft` — error handling

When `dispatch_draft` returns `{"status": "error", ...}`, surface the
`reason` and `detail` to the user. Do **not** claim a card was posted.

Common errors:

| `reason`             | What happened                                         |
| -------------------- | ----------------------------------------------------- |
| `missing_token`      | `ROUTER_INTERNAL_TOKEN` not set in this container     |
| `network_error`      | Could not reach the router at `http://router:8090`    |
| `router_error`       | Router returned a non-200 response (see `detail`)     |
| `invalid_issue_url`  | Issue number could not be parsed from the URL         |
| `missing_slack_context` | `--channel` / `--thread-ts` not set               |

## `dispatch_list_pending_drafts` — recovery

If you believe a card was created but you can't confirm `card_ts`,
call `dispatch_list_pending_drafts` to see your outstanding drafts:

```bash
python /config/packs/dispatch/handler.py dispatch_list_pending_drafts
```

Returns `{"drafts": [...]}`. Each entry has `draft_id`,
`slack_message_ts`, and `payload` so you can confirm the card's
channel/thread with the user.

## `dispatch_health` shape

```json
{
  "cli_version": "2.1.142 (Claude Code)",
  "claude_path": "/usr/local/bin/claude",
  "workspace_volume_writable": true,
  "sonnet_probe_ok": true,
  "sonnet_probe_exit_code": 0
}
```

On failure the relevant field flips to `false` and an extra
`sonnet_probe_detail` (or matching diagnostic) is added. The handler
never raises through to a 500 — that's an acceptance criterion.

The Sonnet probe is pinned to Sonnet on purpose so the smoke check
never burns Opus quota.

## When you don't have it

If `python /config/packs/dispatch/handler.py dispatch_health` reports
`claude_path: null` or `workspace_volume_writable: false`, tell the
operator what's missing and stop — don't try to dispatch.
