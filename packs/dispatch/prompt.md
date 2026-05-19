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

| Verb              | Purpose                                                  |
| ----------------- | -------------------------------------------------------- |
| `dispatch_health` | smoke probe — cli, workspace, sonnet round-trip          |
| `dispatch_issue`  | spawn a `claude -p` against a GitHub issue URL           |
| `dispatch_status` | pool snapshot: running and queued dispatch IDs           |
| `dispatch_cancel` | SIGTERM the process group, tear down workspace           |

## `dispatch_issue` — approval gating (D-7)

When `dispatch_issue` returns `{"status": "approval_required", ...}`,
a human must approve before the work starts. Do **not** spawn the
dispatch yourself. Instead, end your reply with a fenced code block
whose info string is literally `draft-approval` and whose body mirrors
the `preview` payload returned by the handler.

```json
{"status": "approval_required", "draft_id": "a1b2c3d4", "preview": {
  "repo": "bramveen1/ai-dev-team",
  "issue_url": "https://github.com/bramveen1/ai-dev-team/issues/42",
  "branch_target": "main",
  "model": "sonnet",
  "est_workspace_path": "/var/lib/dispatch/dispatch-20260519T120000-abc123",
  "gate_reason": "always"
}}
```

Your reply must end with:

````
```draft-approval
{"draft_id": "a1b2c3d4", "pack": "dispatch", "action_verb": "dispatch_issue", "payload": {
  "repo": "bramveen1/ai-dev-team",
  "issue_url": "https://github.com/bramveen1/ai-dev-team/issues/42",
  "branch_target": "main",
  "model": "sonnet",
  "est_workspace_path": "/var/lib/dispatch/dispatch-20260519T120000-abc123",
  "gate_reason": "always"
}}
```
````

Rules (same as the github pack):
- Info string must be `draft-approval` exactly.
- `draft_id` comes from the handler response — use it verbatim.
- `pack` is `"dispatch"`, `action_verb` is `"dispatch_issue"`.
- Everything else goes into `payload` so the card can preview it.
- One block per draft. No prose after it.

`gate_reason` values and what they mean:

| `gate_reason`         | Why the gate fired                                          |
| --------------------- | ----------------------------------------------------------- |
| `always`              | Pilot mode — every dispatch needs approval                  |
| `destructive_keyword` | `model=opus` and issue text contains a destructive keyword  |
| `cost_threshold`      | 5h window cost ≥ `DISPATCH_APPROVAL_COST_USD` (default $15) |

When `gate_reason` is `cost_threshold`, the preview also includes
`current_window_cost_usd` and `threshold_usd` — include those in the
`payload` so the human sees the numbers on the approval card.

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
