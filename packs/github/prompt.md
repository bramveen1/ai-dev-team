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

Merging a PR requires a human approval card. Do **not** call
`gh pr merge` directly. Instead, end your reply with a fenced code
block whose info string is literally `draft-approval` and whose body
is a single JSON object. The router parses that block, strips it from
the visible message, and posts an approval card with a Merge button.

Concrete example for "merge PR #97":

````
```draft-approval
{"draft_id": "97", "pack": "github", "action_verb": "merge", "payload": {"repo": "bramveen1/ai-dev-team", "pr": 97, "title": "Hotfix: opt Sam into packs: [github]", "base": "main", "head": "hotfix-sam-packs-github"}}
```
````

Notes on the shape:
- The fence info is `draft-approval` — not `json` and not anything
  else. If you write ` ```json `, the router won't see it as a draft
  block and the approval card won't render.
- `draft_id` is required. Use the PR number as a string.
- `pack` is `"github"`. `action_verb` is `"merge"`.
- Everything else (repo, pr, title, base, head, summary, …) goes
  inside `payload` so the approval card can preview it.
- One block per draft. Don't add extra prose after it.

Other write actions (`gh issue create`, `gh issue comment`,
`gh pr review`) are not approval-gated for this pack — execute them
directly when the user asks.

## When you don't have it

If `gh` returns `command not found` or the token is missing, tell the
user to run `grant <agent> github` in Slack. Don't try to call the
GitHub REST API directly as a fallback — the grant flow exists for
exactly this reason.
