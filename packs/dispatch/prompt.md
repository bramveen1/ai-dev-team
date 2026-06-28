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
| `dispatch.draft`              | request human approval for a dispatch                    |
| `dispatch.list_pending_drafts`| list pending drafts (recovery path after Slack failure)  |
| `pr_review`                   | post a formal GitHub review as aidt-tl-sam (one step)   |
| `pr_review_health`            | smoke probe for the pr_review token and identity         |

## `dispatch.draft` — structured approval request

Use `dispatch.draft` whenever you want to dispatch an issue but need
human approval first. This replaces the old `draft-approval` fence
block flow — **do not** emit fence blocks; call the verb instead.

```bash
python /config/packs/dispatch/handler.py dispatch.draft \
  --issue-url https://github.com/bramveen1/ai-dev-team/issues/42 \
  --title "Fix login regression" \
  --model sonnet \
  --persona dev \
  --budget-seconds 1800
```

`--channel`, `--thread-ts`, and `--agent` default to the
`$DISPATCH_CHANNEL`, `$DISPATCH_THREAD_TS`, and `$DISPATCH_AGENT`
environment variables that the router injects into every agent
container, so you normally don't need to pass them.

On success the verb returns:

```json
{
  "status": "draft_created",
  "draft_id": "a1b2c3d4",
  "gate_reason": "always",
  "card_ts": "1705700000.000100"
}
```

The router has already persisted the draft and posted the Block Kit
approval card to Slack. You don't need to do anything else — tell the
user the approval card has been posted and wait for their response.

On failure:

```json
{"status": "error", "reason": "slack_post_failed", "draft_id": "a1b2c3d4", ...}
```

If Slack fails, the draft is still persisted. Use
`dispatch.list_pending_drafts` to surface it to the user.

`gate_reason` values and what they mean:

| `gate_reason`         | Why the gate fired                                          |
| --------------------- | ----------------------------------------------------------- |
| `always`              | Pilot mode — every dispatch needs approval                  |
| `destructive_keyword` | `model=opus` and issue text contains a destructive keyword  |
| `cost_threshold`      | 5h window cost ≥ `DISPATCH_APPROVAL_COST_USD` (default $15) |

## `dispatch.list_pending_drafts` — recovery path

If a previous `dispatch.draft` call returned `slack_post_failed`,
the draft survived in the database. List pending drafts so you can
tell the user what's waiting:

```bash
python /config/packs/dispatch/handler.py dispatch.list_pending_drafts
```

Returns:

```json
{
  "status": "ok",
  "agent": "sam",
  "drafts": [
    {
      "draft_id": "a1b2c3d4",
      "action_verb": "dispatch_issue",
      "slack_channel": "C12345",
      "slack_message_ts": "",
      "created_at": "2026-01-15T12:00:00+00:00",
      "expires_at": "2026-01-15T13:00:00+00:00",
      "payload": {...}
    }
  ]
}
```

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

## `pr_review` — post a formal GitHub PR review (one step)

**Reviewing a PR = call `pr_review`, then paste the returned `html_url` into the Slack thread.**

Do not split reviewing into two separate steps (post Slack verdict, then separately run `gh pr review`). Call `pr_review` once — it runs all precondition checks, posts the formal GitHub review as `aidt-tl-sam`, and returns a receipt. Paste the `html_url` from the receipt into your Slack reply.

```bash
python /config/packs/dispatch/handler.py pr_review \
  --pr-url https://github.com/bramveen1/ai-dev-team/pull/42 \
  --verdict approve \
  --body "LGTM — no blocking findings. Recommend merge."
```

`--verdict` must be one of `approve`, `request-changes`, or `comment`.

On success:

```json
{
  "status": "ok",
  "reviewer": "aidt-tl-sam",
  "verdict": "approve",
  "repo": "bramveen1/ai-dev-team",
  "pr": 42,
  "review_id": 1234567890,
  "html_url": "https://github.com/bramveen1/ai-dev-team/pull/42#pullrequestreview-1234567890"
}
```

Paste the `html_url` into the Slack thread so the team can see the formal review link.

The verb refuses (returns `{status: refused}`) when:
- The `aidt-tl-sam` token is missing or resolves to the wrong identity.
- `aidt-tl-sam` authored or committed any commit on the PR (identity conflict).

Use `--dry-run` to run all checks without posting a review.

`pr_review` is **manual-only** — never call it from an automated loop or a scheduled task.

## When you don't have it

If `python /config/packs/dispatch/handler.py dispatch_health` reports
`claude_path: null` or `workspace_volume_writable: false`, tell the
operator what's missing and stop — don't try to dispatch.
