# Authoring a Pack

A **pack** is a self-contained directory that grants an agent access to one external service (GitHub, Zoho Mail, an internal API, …). Packs replaced the old five-layer capability framework — see [capabilities-simplification.md](capabilities-simplification.md) for the migration history.

This guide is for the engineer adding a new pack. For the day-to-day "give Sam GitHub access" flow, see [managing-agents-from-slack.md](managing-agents-from-slack.md).

## Quick decision: do you need a pack at all?

If the service has a native [Claude.ai connector](https://docs.claude.com/en/docs/claude-code/mcp) (Microsoft 365, Gmail, Google Calendar, Google Drive, Notion, Linear, …) the agent already has access — no pack needed. Just describe **when** to use it and **what** needs approval in the agent's `role.md` and you're done. See Lisa's role for an example.

Write a pack only when the service has no Claude connector (`gh` CLI, Zoho Mail, internal HTTP APIs, PostHog if not connector-backed, …).

## Anatomy

```
packs/<pack-name>/
├── pack.yaml          # required — manifest
├── prompt.md          # appended to the agent's system prompt
├── mcp.json           # optional — merged into --mcp-config
├── authenticate.py    # optional — populates secrets via Slack
└── install.sh         # optional — provisions a host-side CLI
```

Pack name = directory name = `pack.yaml`'s `name` field. Use lowercase-hyphenated.

### `pack.yaml`

The manifest. Tiny on purpose — five fields total.

```yaml
name: github                        # required, must match directory name
description: |
  One-paragraph summary the operator sees in `list packs` from Slack.
needs: [GITHUB_TOKEN]               # secret keys this pack needs at dispatch
cli: gh                             # optional CLI binary on PATH at /opt/tools
approve: [merge]                    # verbs that require human approval
```

- **`needs`** — keys the router injects as env vars when dispatching to an agent that has this pack. The values come from `data/secrets.json[<pack-name>]`, populated by `authenticate.py`. If `needs` is empty, no `authenticate.py` is run during grant.
- **`cli`** — declarative hint that the agent uses a CLI binary. Document where it gets installed in `install.sh`; the router ensures `/opt/tools` is on `PATH` inside agent containers.
- **`approve`** — verbs that require an approval card. The agent emits the verb in its `draft-approval` block; the router compares it to this list.

See [packs/_template/pack.yaml](../packs/_template/pack.yaml) for the annotated template.

### `prompt.md`

Markdown. Appended verbatim to the agent's system prompt at dispatch time, after `WORLDVIEW.md` / `role.md` / `personality.md` / memory. Keep it under ~200 lines — every line costs tokens on every turn.

What to put in it:

1. **What you can do.** "You have access to the `gh` CLI. Use it to read repos, list/comment on issues, …"
2. **The exact `draft-approval` block format** for each verb that requires approval. Show a *concrete copy-pasteable example* — don't reference the schema abstractly. The agent gets confused by markdown-escaped fences. Use a literal example:

   ````markdown
   To merge a PR you must produce this block:

   ```draft-approval
   {"draft_id": "<pr-number>", "pack": "github", "action_verb": "merge", "payload": {"repo": "owner/name", "pr": 97, "title": "...", "base": "main", "head": "..."}}
   ```

   Fence info string is `draft-approval`, NOT `json`. `draft_id`, `pack`,
   `action_verb`, and `payload` are all required. Everything else nests
   inside `payload`.
   ````
3. **What's autonomous vs. what's not.** Reads, lists, comments → no approval. Merges, sends, publishes → approval card. Mirror the `approve:` list.

[packs/github/prompt.md](../packs/github/prompt.md) is the reference.

### `mcp.json` (optional)

A regular Claude Code MCP server config. The router merges it into a temp `--mcp-config` file at dispatch time. Use this when your pack needs MCP tools beyond a CLI.

```json
{
  "mcpServers": {
    "github-mcp": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
    }
  }
}
```

`${GITHUB_TOKEN}` is interpolated from the env vars the router injects (the `needs:` list above). Never paste raw secrets into `mcp.json`.

### `authenticate.py` (optional)

An `async def acquire(say) -> dict` that returns the secret(s) to store. Most packs need this. Two common shapes:

- **Paste-a-token (simplest)** — one DM prompt, validate the result against the service's API, return `{"GITHUB_TOKEN": "..."}`. Used by [packs/github/authenticate.py](../packs/github/authenticate.py).
- **OAuth device-code flow** — for services that support it. The shared helper `router.packs.oauth_devicecode.run_device_flow()` handles polling, presents the verification URL+code in Slack, and returns `{"ACCESS_TOKEN": "...", "REFRESH_TOKEN": "..."}`. Used by [packs/zoho-mail/authenticate.py](../packs/zoho-mail/authenticate.py).

The `say` argument is a `SlackPrompt` (see [router/packs/grants.py](../router/packs/grants.py)) — call `await say(text)` to send a message into the grant thread, and `await say.expect_reply()` to wait for the user's next message in that thread. Store nothing yourself — the router does that.

If your pack has no `authenticate.py` but does have `needs:`, the grant flow falls back to a generic "paste a value for `<KEY>`" prompt per key. Good enough for one-token services; write `authenticate.py` if you want validation.

### `install.sh` (optional)

A shell script the grant flow runs after `authenticate.py` succeeds, before the manifest is updated. Use it to drop a CLI binary into the shared `agent-tools` Docker volume mounted at `/opt/tools` in every agent container.

[packs/github/install.sh](../packs/github/install.sh) is the canonical example — it auto-detects the compose-prefixed volume name and installs `gh` from the official tarball. Crib from it. Idempotent re-runs are the pack author's responsibility.

If your pack is pure-MCP (no CLI), skip `install.sh`.

## Local testing

1. Create the directory: `cp -r packs/_template packs/<your-pack>/`. Edit `pack.yaml` so `name:` matches the dir name.
2. Run `.venv/bin/pytest tests/unit/packs -m unit -v` — the loader's discovery tests pick up the new pack and complain if `pack.yaml` is malformed.
3. To exercise the grant flow against a throwaway test agent, point the test fixtures at your tmp packs dir:
   ```python
   await handle_grant(
       GrantCommand(agent="alex", pack="<your-pack>"),
       say,
       packs_dir=Path("packs"),
       agents_dir=tmp_path / "agents",
       secret_store=SecretStore(tmp_path / "secrets.json"),
   )
   ```
4. End-to-end smoke test: `make up`, `@router grant <agent> <your-pack>` in a Slack DM, walk the auth flow, then DM the agent with a request that uses the pack.

## Removing a pack

Delete the directory. Agents that still list it in `agent.yaml`:`packs:` will log a warning at dispatch (the dispatcher's pack hook silently skips missing packs). Run `@router revoke <agent> <pack>` from Slack first if you want it removed cleanly.

`data/secrets.json` is keyed on pack name — drop the entry by hand, or rely on `revoke` to delete it.
