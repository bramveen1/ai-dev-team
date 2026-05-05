# zoho-mail pack

Zoho Mail access for any agent, via the Maton gateway. Read, search,
draft, send, archive — all over plain HTTPS, no MCP server.

## One-time operator setup

Nothing. The pack uses a paste-and-validate flow for the API key, so
there's no OAuth app to register, no env var to pre-populate, and no
CLI to install.

## Granting an agent

From any Slack DM with an agent (the target agent is named in the
command — you don't have to DM the agent that will *use* the token):

```
grant lisa zoho-mail
```

The flow:

1. Bot: ":envelope_with_arrow: Generate a Maton gateway API key at https://ctrl.maton.ai — paste it as your next message."
2. You generate the key in Maton, scoped to the `zoho-mail` connection, and paste it back into the thread.
3. Bot validates it via `GET /zoho-mail/api/accounts` and replies ":white_check_mark: Token validated against the gateway (N accounts reachable)."
4. Bot stores the token in `data/secrets.json["zoho-mail"]["ZOHO_API_KEY"]` and appends `zoho-mail` to `config/agents/lisa/agent.yaml`.
5. Bot tells you to run `docker compose restart lisa` to pick up the change.

After restart, Lisa has `$ZOHO_API_KEY` in her environment and a
system-prompt section explaining the Maton gateway endpoints (see
[`prompt.md`](prompt.md)).

You can delete the message containing the token right after step 4 —
the value is already in `data/secrets.json` and Slack history is the
only remaining trace.

## Revoking

```
revoke lisa zoho-mail
```

Removes the pack from Lisa's manifest. The stored token stays in
`data/secrets.json` so a re-grant doesn't require a re-paste; delete
the `zoho-mail` block manually if you want to evict it. To make the
token itself unusable, rotate it from https://ctrl.maton.ai.

## Approval rules

`pack.yaml` declares `approve: []`. There are no approval-gated verbs
— the grant *is* the approval. The agent can read, draft, send, and
archive freely on the granted account.

That's appropriate for the *self-control* case (e.g. Lisa managing
her own inbox). If you ever want an agent to operate on someone
else's inbox, author a separate pack with explicit `approve:` rules.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `:x: Setup for zoho-mail failed: Maton rejected the token (401)` | Key was wrong, expired, or wrong scope | Generate a new one at https://ctrl.maton.ai scoped to `zoho-mail` |
| `:x: Setup for zoho-mail failed: Maton returned zero accounts` | The key works but no Zoho mailbox is attached | Connect the Zoho mailbox in the Maton dashboard, then re-grant |
| `:x: Setup for zoho-mail failed: no token received` | Replied with empty/whitespace | Re-run `grant`, paste the token verbatim |
| Agent gets 401 on every call after grant | Token was rotated or revoked | Run `revoke lisa zoho-mail` then `grant lisa zoho-mail` again |
