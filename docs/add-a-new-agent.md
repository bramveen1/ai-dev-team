# Runbook: Add a New Agent

> ⚠️ **Sections below describe the old capability framework.** Capabilities → packs migration ([capabilities-simplification.md](capabilities-simplification.md)) replaced the per-agent `capabilities:` block with a `packs:` list. The wizard now prompts for packs (multi-select over `packs/*`), not capability instances. Use the TL;DR; ignore the rest until someone rewrites this page. For pack management see [managing-agents-from-slack.md](managing-agents-from-slack.md).

## TL;DR — use the wizard

```bash
make add-agent
```

The wizard prompts for the agent's id, display name, role/personality, optional packs, and (optionally) Slack tokens, then writes everything in the right places: `config/agents/<name>/{agent.yaml,role.md,personality.md}`, a pre-filled `slack-manifests/<name>.yaml`, the `.env` token block, and a freshly regenerated `docker-compose.yml`. Total time: 2-3 minutes for a simple agent.

Once the wizard finishes:
1. Create the Slack app — paste `slack-manifests/<name>.yaml` at <https://api.slack.com/apps> and copy the 3 tokens into `.env` (the wizard left placeholders if you didn't paste them).
2. `make up` to bring up the new container.
3. From Slack: `@router grant <agent> <pack>` for each pack you pre-selected (provisions the secrets).
4. DM the agent in Slack.

For scripted / non-interactive use (e.g. restoring from a copied `config/` folder):

```bash
python -m scripts.add_agent --from-yaml fixtures/maya.yaml --no-slack
```

The rest of this runbook is the **manual** path — useful for understanding what the wizard does, or for debugging if it errors out.

---

## Prerequisites

- You have admin rights on the Slack workspace.
- Docker and Docker Compose are set up locally (`docker compose ps` works).
- You've read [capability-framework.md](capability-framework.md) at least once.
- Pick the agent name (lowercase, no spaces). Used as both the container name and config key. Examples: `alex`, `sam`, `dave`, `maya`, `lin`.

## Checklist

- [ ] 1. Create the Slack app from a manifest
- [ ] 2. Add bot tokens to `.env`
- [ ] 3. Create `config/agents/<name>/role.md`
- [ ] 4. Create `config/agents/<name>/personality.md`
- [ ] 5. Create `config/agents/<name>/agent.yaml` (identity, capabilities, scheduled tasks)
- [ ] 6. Run `make compose` to regenerate `docker-compose.yml`
- [ ] 7. Start the container and run smoke tests
- [ ] 8. Update [agents.md](agents.md) with the new roster entry
- [ ] 9. Add/update tests

> **Where the file edits live:** Each agent is now defined by a single manifest at `config/agents/<name>/agent.yaml`. The router auto-discovers agents at startup (#74); `docker-compose.yml` is generated from those manifests (#75); `make add-agent` walks the wizard for you (#76).

---

## 1. Create the Slack app from a manifest

Each agent is a separate Slack bot with its own tokens. Use a manifest to create it reproducibly.

> **Slash command name:** Slack scopes slash command ownership workspace-wide — if two apps register `/tasks` in the same workspace, only the most recently installed one receives the command. Always use a per-agent name like `/<name>-tasks`. If a dev deployment shares the workspace with prod, prefix the dev side (e.g. `/dev-<name>-tasks`) and set `SLASH_COMMAND_PREFIX=dev-` in the dev `.env` so the router registers the matching handler.

### 1a. Prepare the manifest

Save as `slack-manifests/<name>.yaml` (create the folder if it doesn't exist):

```yaml
display_information:
  name: "<Name>"
  description: "<One-line role description, e.g. 'Growth marketer agent'>"
  background_color: "#4A154B"
features:
  bot_user:
    display_name: "<Name>"
    always_online: true
  slash_commands:
    - command: /<name>-tasks  # e.g. /lisa-tasks; prefix with dev- for dev apps
      description: Manage scheduled agent tasks
      usage_hint: "[list | create | pause <id> | resume <id> | delete <id>]"
      should_escape: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - channels:read
      - chat:write
      - commands
      - groups:history
      - groups:read
      - im:history
      - im:read
      - im:write
      - reactions:write
      - users:read
      - assistant:write
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.groups
      - message.im
      - assistant_thread_started
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
  token_rotation_enabled: false
```

### 1b. Create the app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick your workspace, paste the manifest, click **Create**.
3. Under **Basic Information**, generate an **App-Level Token** with the `connections:write` scope. Copy it (`xapp-...`).
4. Under **OAuth & Permissions**, click **Install to Workspace**, approve. Copy the **Bot User OAuth Token** (`xoxb-...`).
5. Under **Basic Information → Signing Secret**, copy it.

## 2. Add bot tokens to `.env`

Add three environment variables following the `LISA_*` pattern (uppercase agent name):

```bash
# .env
<NAME>_BOT_TOKEN=xoxb-...
<NAME>_APP_TOKEN=xapp-...
<NAME>_SIGNING_SECRET=...
```

Also update `.env.example` with placeholders so other contributors know the vars exist.

## 3. Create `config/agents/<name>/role.md`

The role file is the agent's job description — what they are responsible for. Keep it crisp (under 150 lines).

```markdown
# <Name> — <Role Title>

You are <Name>, the <role> for the ai-dev-team.

## Responsibilities

- <Responsibility 1>
- <Responsibility 2>
- <Responsibility 3>

## Working Style

- <How you decide priorities>
- <When you escalate>
- <Who you coordinate with>

## Capabilities

You have the following capabilities (auto-rendered in the system prompt — reference
them by name, do not re-describe providers here):

- `email_<instance>` — <what you do with it>
- `calendar_<instance>` — <what you do with it>
- `<capability>_<instance>` — <what you do with it>

## Approval Rules

- <When to create a draft vs. act directly>
- <Who approves what>
```

Tip: copy `config.example/agents/lisa/role.md` as a starting point.

## 4. Create `config/agents/<name>/personality.md`

Short. Voice only, not behaviour. Example:

```markdown
# <Name> — Personality

You are <descriptor>. You default to <action style>.
You speak in <voice>. <Sentence-length guidance>. <Vocabulary notes>.
```

## 5. Create `config/agents/<name>/agent.yaml`

This single file is the agent's manifest — identity, capabilities, and any scheduled tasks. The router auto-discovers it at startup; no `AGENT_MAP` edit is needed.

```yaml
# config/agents/<name>/agent.yaml
name: <Name>                          # display name (shown in Slack)
container: <name>                     # docker container; default = directory name
thinking_status: "is <verb>…"

capabilities:
  email:
    - instance: mine
      provider: gmail-connector
      account: <name>@pathtohired.com
      ownership: self
      permissions: [read, send, draft-create, draft-update, draft-delete]
  # add more capabilities as needed

scheduled_tasks:
  # Optional. Each entry becomes a seed task on first boot (idempotent — keyed
  # by (agent_name, name)). Enable via /tasks resume <id> in Slack.
  - name: Daily standup digest
    prompt: "Summarize yesterday's standup into a 5-bullet status."
    schedule_cron: "0 9 * * 1-5"
    enabled: false
```

Pick capabilities from the existing catalogue (see [capability-framework.md](capability-framework.md)). For each instance, decide:
- **Instance name** (`mine`, `bram`, `team`, etc.)
- **Provider** (must exist in `config/providers.yaml`)
- **Account** (email, workspace, org)
- **Ownership** (`self` | `delegate` | `shared`)
- **Permissions** (allowlist from the capability's vocabulary)

Baseline capabilities (`web`, `memory`, `slack_io`, `scheduled_tasks`) are auto-merged from `config/baseline.yaml` — don't redeclare them.

Verify the config loads and renders:

```bash
python -m capabilities render <name>
python -m capabilities mcp_config <name>
```

The first prints the capabilities summary that will be injected into the system prompt. The second prints the generated `.mcp.json`. If either errors, fix the config before continuing.

## 6. Render `docker-compose.yml`

`docker-compose.yml` is generated from `config/agents/*/agent.yaml` plus the router service stub — there is no per-service block to hand-edit, and there are no per-agent `Dockerfile`s by default (every agent uses the shared `ai-dev-team-base:latest` image). Regenerate after adding the agent dir:

```bash
make compose                          # or: python -m scripts.render_compose
```

Local-only tweaks (a debug port, a host volume mount) belong in `docker-compose.override.yml` — Compose merges it automatically and the renderer never touches it.

If your agent needs extra CLI tools (a Python MCP binary, a Node package), drop a `Dockerfile` next to its manifest at `config/agents/<name>/Dockerfile`. The renderer detects it and switches that service to `build:` mode.

## 7. Start the container and run smoke tests

```bash
# Build and start (also re-renders compose, via the Makefile target)
make up

# Authenticate Claude Code in the new container (Max subscription path)
docker exec -it <name> claude auth login --claudeai

# Verify container is up and Claude Code is available
docker exec -u claude <name> claude --version
docker compose ps
```

### Smoke test in Slack

- [ ] DM the agent — it replies.
- [ ] Mention the agent in a channel (`@<Name> ping`) — it replies.
- [ ] The reply comes as a thread under the mention.
- [ ] Ask the agent "what are your capabilities?" — it lists the instances declared in `capabilities.yaml`.
- [ ] If the agent has a delegate email capability, ask it to draft a message — a draft appears (Slack approval card shows "Open in <app>").
- [ ] Run `/tasks list` in a DM with the agent — the slash command responds.
- [ ] In a channel where another agent's bot is also installed, mention `@<Name>` — only the new agent replies (the other doesn't double-handle the message).

### Smoke test via router logs

```bash
docker compose logs -f router | grep -i <name>
```

You should see session lifecycle logs when you DM the agent.

## 8. Update `docs/agents.md`

Add a new section with:
- Role title and one-line description
- Capabilities (table: capability / instance / provider / ownership / permissions)
- Seed scheduled tasks (if any)

Keep [docs/agents.md](agents.md) the single source of truth for "who's on the team today." Update it every time an agent is added, removed, or has a capability change.

## 9. Tests

Add or update tests to cover the new agent. **Do not** ship an agent without tests for the dispatch and config paths.

### Required

- **`tests/unit/test_config.py`** — assert `AGENT_MAP` contains `<name>` and the paths are strings.
- **`tests/unit/capabilities/test_loader.py`** — load `config/capabilities.yaml` for the new agent and assert the expected instances resolve.
- **`tests/unit/capabilities/test_prompt_renderer.py`** — snapshot test of the rendered capabilities summary for the new agent.
- **`tests/unit/test_mentions.py`** — add the new agent name to the fixture map; assert mentions resolve to it.

### Recommended

- **`tests/unit/test_dispatcher.py`** — assert dispatching to `<name>` uses the right container name.
- **`tests/integration/test_router_flow.py`** — add a case that simulates a Slack event directed at the new agent.
- **`tests/unit/scheduled_tasks/test_seeds.py`** — if you added seed tasks, assert they are inserted on fresh start and skipped on re-seed.

### Run before opening a PR

```bash
.venv/bin/pytest tests/unit -m unit -v
.venv/bin/pytest tests/integration -m integration -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

All four must pass.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Slack "dispatch_failed" on mention | Bot tokens not in `.env`, or `docker-compose.yml` is stale | Add the `<NAME>_*` trio to `.env`, then run `make compose && docker compose up -d` so the router service picks them up |
| `python -m capabilities render <name>` says "agent not found" | Missing `config/agents/<name>/agent.yaml`, or `capabilities` block is empty | Create the manifest; double-check the indent of the `capabilities:` block |
| Agent DMs work but `@mention` doesn't | Bot user map not rebuilt | Restart the router; it rebuilds on startup |
| `docker compose up` fails with "image not found" | Base image not built | Run `docker build -t ai-dev-team-base:latest -f docker/Dockerfile.base .` first |
| Tests pass locally but fail in CI with "config not found" | Tests reference `config/` which is gitignored | Point tests at `config.example/` or at `tmp_path` fixtures |
