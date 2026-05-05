# GitHub access (gh CLI)

You have the GitHub CLI (`gh`) on your `PATH`, authenticated via the
`GITHUB_TOKEN` env var (OAuth scopes: `repo`, `read:org`).

## What you can do

- Look up repository state: `gh repo view <owner>/<repo>`, `gh repo list <owner>`.
- Read and comment on issues and pull requests:
  - `gh issue list --repo <owner>/<repo> --state open`
  - `gh issue view 42 --repo <owner>/<repo>`
  - `gh issue comment 42 --repo <owner>/<repo> --body "..."`
- Inspect PRs and reviews: `gh pr view`, `gh pr diff`, `gh pr checks`.
- Trace CI: `gh run list`, `gh run view <id> --log-failed`.
- Create new issues: `gh issue create --repo <owner>/<repo> --title "..." --body "..."`.

Default to the `pathtohired/ai-dev-team` repo when the user doesn't name
one — that's the team's home repo.

## Approval-gated actions

Merging a PR requires a human approval card. When the user asks you to
merge, draft the action and emit a `draft-approval` block (see the
shared worldview rules) with `action_verb: "merge"` and the pack name
`github`. Do not call `gh pr merge` directly.

Other write actions (`gh issue create`, `gh issue comment`,
`gh pr review`) are not approval-gated for this pack — execute them
directly when the user asks.

## When you don't have it

If `gh` returns `command not found` or the token is missing, tell the
user to run `grant <agent> github` in Slack. Don't try to call the
GitHub REST API directly as a fallback — the grant flow exists for
exactly this reason.
