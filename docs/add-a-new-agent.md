# Runbook: Add a New Agent

Walk-through for adding a new agent to the team (e.g. Alex, Sam, Dave). Budget: **under one hour** for a simple agent that reuses existing capabilities.

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
- [ ] 5. Add a capabilities section to `config/capabilities.yaml`
- [ ] 6. Register the agent in `router/config.py` (`AGENT_MAP`)
- [ ] 7. Add a Dockerfile at `agents/<name>/Dockerfile`
- [ ] 8. Add the service to `docker-compose.yml`
- [ ] 9. (Optional) Seed scheduled tasks in `router/scheduled_tasks/seeds.py`
- [ ] 10. Start the container and run smoke tests
- [ ] 11. Update [agents.md](agents.md) with the new roster entry
- [ ] 12. Add/update tests

---

## 1. Create the Slack app from a manifest

Each agent is a separate Slack bot with its own tokens. Use a manifest to create it reproducibly.

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
    - command: /tasks
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

## 5. Add a capabilities section to `config/capabilities.yaml`

Pick capabilities from the existing catalogue (see [capability-framework.md](capability-framework.md)). For each one, decide:
- **Instance name** (`mine`, `bram`, `team`, etc.)
- **Provider** (must exist in `config/providers.yaml`)
- **Account** (email, workspace, org)
- **Ownership** (`self` | `delegate` | `shared`)
- **Permissions** (allowlist from the capability's vocabulary)

```yaml
# config/capabilities.yaml
agents:
  <name>:
    agent: <name>
    capabilities:
      email:
        - instance: mine
          provider: gmail-connector
          account: <name>@pathtohired.com
          ownership: self
          permissions: [read, send, draft-create, draft-update, draft-delete]
      # add more capabilities as needed
```

Baseline capabilities (`web`, `memory`, `slack_io`, `scheduled_tasks`) are auto-merged from `config/baseline.yaml` — don't redeclare them.

Verify the config loads and renders:

```bash
python -m capabilities render <name>
python -m capabilities mcp_config <name>
```

The first prints the capabilities summary that will be injected into the system prompt. The second prints the generated `.mcp.json`. If either errors, fix the config before continuing.

## 6. Register the agent in `router/config.py`

Add an entry to `AGENT_MAP` in `router/config.py`:

```python
AGENT_MAP = {
    "lisa": { ... },
    "<name>": {
        "name": "<Name>",
        "container": "<name>",
        "role_file": "config/agents/<name>/role.md",
        "personality_file": "config/agents/<name>/personality.md",
        "thinking_status": "is <verb>\u2026",  # shown in Slack while thinking
    },
}
```

The router uses this map to resolve `@mention` → agent, dispatch to the right container, and load role/personality files.

## 7. Add a Dockerfile at `agents/<name>/Dockerfile`

Most agents need only the base image. Copy `agents/lisa/Dockerfile`:

```dockerfile
FROM ai-dev-team-base:latest

# <Name> uses the base image only.
# Add provider-specific binaries here if the agent needs any.

CMD ["sleep", "infinity"]
```

If the agent needs extra CLI tools (e.g. a Python MCP binary, a Node package), add the `apt-get install` or `npm install -g` lines here.

## 8. Add the service to `docker-compose.yml`

Under `services:`, add a block mirroring `lisa`, and wire the bot token env vars into the router:

```yaml
  <name>:
    build:
      context: ./agents/<name>
      dockerfile: Dockerfile
    container_name: <name>
    environment:
      - CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
    volumes:
      - ./config:/config
      - ./systems:/systems
      - <name>-claude-config:/home/claude/.claude
    deploy:
      resources:
        limits:
          memory: 512m
    restart: unless-stopped
```

And add the volume at the bottom:

```yaml
volumes:
  lisa-claude-config:
  <name>-claude-config:
```

Pass the agent's Slack tokens to the router:

```yaml
  router:
    environment:
      - LISA_BOT_TOKEN=${LISA_BOT_TOKEN}
      - <NAME>_BOT_TOKEN=${<NAME>_BOT_TOKEN}
      # ... etc.
    depends_on:
      - lisa
      - <name>
```

## 9. (Optional) Seed scheduled tasks

If the agent has recurring work (daily inbox review, weekly digest), add entries to `DEFAULT_SEED_TASKS` in `router/scheduled_tasks/seeds.py`:

```python
SeedTask(
    agent_name="<name>",
    name="<Task label>",
    prompt="<The prompt the agent receives on each run>",
    schedule_cron="0 9 * * 1-5",  # 9 AM UTC weekdays
    enabled=False,  # off by default — enable via /tasks resume
),
```

Seeds are inserted idempotently on startup (keyed by `(agent_name, name)`).

## 10. Start the container and run smoke tests

```bash
# Build and start
docker compose up --build -d

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

### Smoke test via router logs

```bash
docker compose logs -f router | grep -i <name>
```

You should see session lifecycle logs when you DM the agent.

## 11. Update `docs/agents.md`

Add a new section with:
- Role title and one-line description
- Capabilities (table: capability / instance / provider / ownership / permissions)
- Seed scheduled tasks (if any)

Keep [docs/agents.md](agents.md) the single source of truth for "who's on the team today." Update it every time an agent is added, removed, or has a capability change.

## 12. Tests

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
| Slack "dispatch_failed" on mention | Bot tokens not wired into `docker-compose.yml` router env | Add `<NAME>_BOT_TOKEN` etc. to the router service |
| `python -m capabilities render <name>` says "agent not found" | Missing entry under `agents:` in `config/capabilities.yaml` | Add the section; check indent |
| Agent DMs work but `@mention` doesn't | Bot user map not rebuilt | Restart the router; it rebuilds on startup |
| `docker compose up` fails with "image not found" | Base image not built | Run `docker build -t ai-dev-team-base:latest -f docker/base.Dockerfile .` first |
| Tests pass locally but fail in CI with "config not found" | Tests reference `config/` which is gitignored | Point tests at `config.example/` or at `tmp_path` fixtures |
