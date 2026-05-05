# github pack

GitHub access for any agent, via the `gh` CLI.

## One-time operator setup

Install the `gh` CLI into the shared `agent-tools` Docker volume so every
agent container has it on `PATH`:

```bash
packs/github/install.sh
```

That's it — the pack uses Personal Access Tokens, so there's no OAuth
app to register and no env var to add.

## Granting an agent

From any Slack DM with an agent (the target agent is named in the
command — you don't have to DM the agent that will *use* the token):

```
grant sam github
```

The flow:

1. Bot: ":key: Generate a GitHub PAT at https://github.com/settings/tokens/new — required scopes: `repo, read:org`. Paste the token as your next message in this thread."
2. You generate the token at GitHub and paste it back into the same thread.
3. Bot validates it via `GET /user` and replies ":white_check_mark: Token validated for `@<your-login>`."
4. Bot stores the token in `data/secrets.json["github"]["GITHUB_TOKEN"]` and appends `github` to `config/agents/sam/agent.yaml`.
5. Bot tells you to run `docker compose restart sam` to pick up the change.

After restart, Sam has `gh` available, the `GITHUB_TOKEN` env var set,
and a system prompt explaining the allowed actions (see [`prompt.md`](prompt.md)).

You can delete the message with the PAT right after step 4 — the token
is already stored and Slack history is the only remaining trace.

## Revoking

```
revoke sam github
```

Removes the pack from Sam's manifest. The stored token stays in
`data/secrets.json` so you can re-grant without re-pasting; delete the
`github` block manually if you want to evict it. To make the token
itself unusable, revoke it from
https://github.com/settings/tokens.

## Approval rules

`pack.yaml` declares `approve: [merge]`. Sam emits an approval card
instead of running `gh pr merge` directly when the user asks him to
merge a PR. Other write actions (`gh issue create`, `gh issue comment`,
`gh pr review`) are not approval-gated.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `:x: Setup for github failed: GitHub rejected the token (401)` | PAT was wrong or already expired | Generate a new one at https://github.com/settings/tokens/new |
| `:x: Setup for github failed: no token received` | Replied with empty/whitespace | Re-run `grant`, paste the token verbatim |
| `gh: command not found` after grant | `install.sh` wasn't run | Run `packs/github/install.sh`, then `docker compose restart sam` |
