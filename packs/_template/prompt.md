# <Pack name>

Brief, agent-facing description of the capability. Imagine the agent reads
this once at the start of every session — what does it need to know?

## What you have

Example:

> You can use the `gh` CLI to query GitHub repositories. The token in
> `$GITHUB_TOKEN` has read+write on the `pathtohired` org's `ai-dev-team`
> repo, plus read on the wider org.

## When to use it

Example:

> Use this when the user asks about repository state, issues, or PRs. Prefer
> `gh` over the GitHub API directly — it handles auth and pagination.

## Approval rules

If `pack.yaml` declares `approve: [...]`, document the rules here so the
agent knows to draft instead of execute.

> Merging PRs requires approval. Drafts of merge actions go through Slack
> first; you'll get a confirmation back before the merge actually runs.
