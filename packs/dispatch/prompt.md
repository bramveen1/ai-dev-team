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

| Verb              | Status (D-1) | Purpose                                                  |
| ----------------- | ------------ | -------------------------------------------------------- |
| `dispatch_health` | implemented  | smoke probe — cli, workspace, sonnet round-trip          |
| `dispatch_issue`  | D-2          | spawn a `claude -p` against a GitHub issue URL           |
| `dispatch_status` | D-2          | read latest JSON event for an in-flight `dispatch_id`    |
| `dispatch_cancel` | D-4          | SIGTERM the process group, tear down workspace          |

Only `dispatch_health` is wired in this scaffold (#D-1). Reach for the
other three after their issues land — they are intentionally absent
until then.

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
