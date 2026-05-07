# brevo pack

Brevo access for any agent, via the Maton gateway. Send transactional
and marketing email, manage contacts and lists, trigger campaigns —
all over plain HTTPS, no MCP server.

## One-time operator setup

Nothing. The pack uses a paste-and-validate flow for the API key, so
there's no OAuth app to register, no env var to pre-populate, and no
CLI to install.

## Granting an agent

From any Slack DM with an agent (the target agent is named in the
command — you don't have to DM the agent that will *use* the token):

```
grant lisa brevo
```

The flow:

1. Bot: ":envelope_with_arrow: Generate a Maton gateway API key at https://ctrl.maton.ai — paste it as your next message."
2. You generate the key in Maton, scoped to the `brevo` connection, and paste it back into the thread.
3. Bot validates it via `GET /brevo/v3/account` and replies ":white_check_mark: Token validated against the gateway (<email> — <company>)."
4. Bot stores the token in `data/secrets.json["brevo"]["BREVO_API_KEY"]` and appends `brevo` to `config/agents/lisa/agent.yaml`.
5. Bot tells you to run `docker compose restart lisa` to pick up the change.

After restart, Lisa has `$BREVO_API_KEY` in her environment and a
system-prompt section explaining the Maton gateway endpoints (see
[`prompt.md`](prompt.md)).

You can delete the message containing the token right after step 4 —
the value is already in `data/secrets.json` and Slack history is the
only remaining trace.

## Revoking

```
revoke lisa brevo
```

Removes the pack from Lisa's manifest. The stored token stays in
`data/secrets.json` so a re-grant doesn't require a re-paste; delete
the `brevo` block manually if you want to evict it. To make the token
itself unusable, rotate it from https://ctrl.maton.ai.

## Approval rules

`pack.yaml` declares `approve: [send-campaign, send-bulk]`. The agent
emits a `draft-approval` block instead of calling the API directly
when:

- It creates a new email campaign (`POST /v3/emailCampaigns`) or
  triggers an existing one (`POST /v3/emailCampaigns/{id}/sendNow`)
  → `action_verb: "send-campaign"`.
- It calls `POST /v3/smtp/email` and the payload fans out — multiple
  `to` recipients, `messageVersions[]`, or a contact list/segment
  reference → `action_verb: "send-bulk"`.

Everything else — reads, single-contact CRUD, and 1:1 transactional
sends through `/v3/smtp/email` — runs without an approval card. This
matches Bram's intent: agents handle 1:1 outreach freely, but a human
signs off before any blast.

The approval card payload includes the resolved recipient count and
campaign id/name (where applicable) so the reviewer can see blast size
before clicking Send. See [`prompt.md`](prompt.md#draft-block-shape)
for the exact JSON shape.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `:x: Setup for brevo failed: Maton rejected the token (401)` | Key was wrong, expired, or wrong scope | Generate a new one at https://ctrl.maton.ai scoped to `brevo` |
| `:x: Setup for brevo failed: unexpected payload shape` | Gateway returned something other than the Brevo `/v3/account` body | Check the Maton dashboard — the connection may not be wired to a Brevo account yet |
| `:x: Setup for brevo failed: no token received` | Replied with empty/whitespace | Re-run `grant`, paste the token verbatim |
| Agent gets 401 on every call after grant | Token was rotated or revoked | Run `revoke lisa brevo` then `grant lisa brevo` again |
| Agent keeps drafting for 1:1 sends | It's misreading the bulk rule | Reinforce in chat: a single `to` with no list/segment is *not* gated |
