# github pack

GitHub access for any agent, via the `gh` CLI.

## One-time operator setup

### 1. Register a GitHub OAuth App

1. Go to https://github.com/settings/applications/new.
2. Application name: anything (e.g. "AI Dev Team Router").
3. Homepage URL / Authorization callback URL: anything — they're unused
   for the device-code flow.
4. After creation, open the app and **enable Device Flow** (checkbox in
   General settings).
5. Copy the **Client ID**.

### 2. Configure the router

Add to `.env`:

```bash
GITHUB_CLIENT_ID=Iv1.xxxxxxxxxxxxxxxx
```

Then restart the router so it picks up the new env var:

```bash
docker compose restart router
```

### 3. Install the `gh` CLI into the shared agent-tools volume

```bash
packs/github/install.sh
```

This is a one-shot — it drops the `gh` binary into the `agent-tools`
Docker volume that's already mounted into every agent container at
`/opt/tools`. Re-run to upgrade.

## Granting an agent

From any Slack DM with one of the agents:

```
grant sam github
```

The router walks the GitHub device-code flow in the same Slack thread:

1. Posts a verification URL + code: "Visit https://github.com/login/device
   and enter code `XXXX-XXXX`."
2. You authorize in your browser.
3. Router stores the token in `data/secrets.json["github"]["GITHUB_TOKEN"]`,
   appends `github` to `config/agents/sam/agent.yaml` `packs:` list.
4. Router tells you to run `docker compose restart sam` to pick up the
   change.

After restart, Sam has `gh` available and the system prompt explains the
allowed actions (see [`prompt.md`](prompt.md)).

## Revoking

```
revoke sam github
```

Removes the pack from Sam's manifest. To also drop the stored token,
delete the `github` block from `data/secrets.json` manually (the token
itself is still valid on GitHub until you revoke it from the OAuth app
settings).

## Approval rules

`pack.yaml` declares `approve: [merge]`. When Sam goes to merge a PR he
emits a draft-approval block instead of executing the merge directly.
The approval flow (PR 6) reads this list to decide.

Other write actions (issue create, PR comment, review) are not
approval-gated — Sam executes them directly.
