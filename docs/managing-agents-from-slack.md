# Managing Agents from Slack

Day-to-day, you grant and revoke an agent's access to outside services entirely from Slack. No terminal, no editing files. Four commands.

> **Where to type these:** DM the *router* bot (not Lisa or Sam) — or mention `@router` in a channel where it's invited. The agent bots themselves (Lisa, Sam) don't accept these commands.

## The four commands

| Command | What it does |
|---|---|
| `list packs` | Shows everything an agent could be granted. |
| `who has <pack>` | Lists which agents currently have a given pack. |
| `grant <agent> <pack>` | Walks you through hooking the agent up to a service. |
| `revoke <agent> <pack>` | Takes the access away. |

A "pack" is one bundle of access to one service — `github`, `zoho-mail`, etc. Agent names are lowercase (`sam`, `lisa`). Pack names are lowercase-hyphenated.

## Granting access — the happy path

```
You:    @router grant sam github
Router: Setting up `github` for `sam`…
Router: :key: Generate a GitHub PAT at https://github.com/settings/tokens/new
        • Required scopes: repo, read:org
        • Set an expiry that fits your security policy (90 days is fine)

        Paste the token as your next message in this thread.
        I'll validate it and store it.
You:    ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Router: Token validated. Stored.
Router: Running `github/install.sh` to provision the CLI…
Router: :white_check_mark: Granted `github` to `sam`.
        Run `docker compose restart sam` to pick up the change.
```

That last line — *restart the container* — is currently manual. Until you do that, Sam's running container hasn't seen the new pack and won't be able to use it.

## What each pack actually asks for

Different services have different setup steps. The router runs the pack's `authenticate.py` to walk you through it. Common shapes:

- **Paste-a-token** (GitHub, internal APIs): the bot DMs a link, you generate a token at the linked page, you paste it back.
- **OAuth device flow** (Zoho, anything Microsoft-style): the bot DMs a code and a verification URL. You open the URL, type the code, approve. The bot polls the service and confirms when it's done.
- **No setup needed** (rare — pure Claude-connector packs): the grant just edits the manifest and you're done.

If a pack has a CLI binary (e.g. `gh`), the router also runs the pack's `install.sh` after auth succeeds. That step downloads the CLI into a shared volume so every agent container can use it.

## Revoking access

```
You:    @router revoke sam github
Router: :white_check_mark: Revoked `github` from `sam`.
        Run `docker compose restart sam` to pick up the change.
```

Revoke removes the pack from `sam/agent.yaml`. The token stays in the secret store unless you explicitly drop it (engineer-only, by deleting the entry from `data/secrets.json`). That's by design — you can re-grant without re-running the auth flow if it was working before.

## Discovery commands

```
You:    @router list packs
Router: Available packs (2):
        • `github` — GitHub access via the gh CLI. ...
        • `zoho-mail` — Zoho Mail outbound (send-on-behalf-of-Bram).
```

```
You:    @router who has github
Router: Agents with `github`: `sam`.
```

## Things that go wrong (and what to do)

**"I clicked the link but it didn't work."**
Usually the token paste-back step. Re-run `@router grant sam github` — the flow is idempotent. If the manifest already has the pack but the token's missing or stale, the router re-runs auth instead of bailing.

**"I pasted the token but the bot says it's invalid."**
Generate a fresh one. The bot validates against the service's API before storing — if it rejected it, the API rejected it. Double-check the *scopes* (the doc each pack DMs you tells you exactly which scopes are required).

**"I granted the pack but the agent says it can't use it."**
You skipped the restart. Run `docker compose restart <agent>` on the host. Agents read their pack list at container start, so a running container won't see a freshly granted pack until it's restarted.

**"`grant` says the pack isn't found."**
The pack name has to match a directory under `packs/` in the repo. Run `@router list packs` to see the spelling. If the pack you want doesn't exist, an engineer needs to author it — see [authoring-a-pack.md](authoring-a-pack.md).

**"`grant` says the agent isn't found."**
The agent name has to match a directory under `config/agents/`. Names are lowercase. Run `@router status` (or check the running containers) to see who's on the team.

**The bot didn't respond at all.**
Either the router container isn't running, or you DMed the wrong bot. The pack commands only work on the *router*. Try DMing the router directly rather than mentioning it.

## What gets created behind the scenes

After a successful `grant sam github`, you can verify on the host:

```bash
cat config/agents/sam/agent.yaml      # `packs:` list now contains `github`
cat data/secrets.json | jq .github    # the encrypted token block
```

You should never have to edit either of these by hand — the grant/revoke commands keep them in sync. If you find yourself wanting to, that's a sign the engineer should add another command, not that you should reach for `vi`.
