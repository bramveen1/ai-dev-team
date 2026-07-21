# ai-dev-team

A multi-agent AI dev team orchestrated via Slack. A router service receives Slack events and dispatches work to specialist agents (Lisa, etc.) running Claude Code CLI in Docker containers.

## Prerequisites

- Docker and Docker Compose
- A Slack app configured with Socket Mode, bot token, and app-level token
- Claude Code CLI authentication (API key or Max subscription)

## Setup

1. **Clone the repo and configure environment variables:**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and fill in your Slack credentials:

   ```
   LISA_BOT_TOKEN=xoxb-...
   LISA_APP_TOKEN=xapp-...
   LISA_SIGNING_SECRET=...
   ```

   **Optional — Discord:** to run agents on Discord as well as Slack, add to `.env`:
   ```
   DISCORD_ENABLED=true
   SAM_DISCORD_BOT_TOKEN=...
   LISA_DISCORD_BOT_TOKEN=...
   ```
   Each token is a Discord bot token (one bot application per agent). Enable the
   **MESSAGE CONTENT** privileged intent for each bot in the Discord Developer Portal.
   When `DISCORD_ENABLED` is unset or `false`, the Discord path is fully skipped and
   Slack is unaffected.

2. **Set up Claude Code authentication** — set `CLAUDE_AUTH_MODE` in your `.env` (choose one):

   | Mode | When to use | `.env` setting |
   |---|---|---|
   | `credentials` *(default)* | Interactive Max login, one-time per host | *(unset or `CLAUDE_AUTH_MODE=credentials`)* |
   | `oauth_token` | Headless/CI, bills Max quota | `CLAUDE_AUTH_MODE=oauth_token` + `CLAUDE_CODE_OAUTH_TOKEN=<token>` |
   | `api_key` | API-credit billing | `CLAUDE_AUTH_MODE=api_key` + `ANTHROPIC_API_KEY=sk-ant-api03-...` |

   **`credentials` mode** (default): after starting the containers, run once per agent:
   ```bash
   docker exec -it lisa claude auth login --claudeai
   ```
   Credentials persist in the `lisa-claude-config` Docker volume.

   **`oauth_token` mode**: generate a long-lived token on the operator's machine:
   ```bash
   claude setup-token   # prints CLAUDE_CODE_OAUTH_TOKEN; add it to .env
   ```
   This mode bills against the Max subscription quota (not metered API credits) and
   is headless-friendly — no interactive login step needed per host.

   The entrypoint hard-fails at startup if the required secret for the selected mode
   is missing (no silent fallback, no cross-mode credential leakage).

3. **Start the system:**

   ```bash
   docker compose up --build
   ```

   This starts:
   - **router** — Python service that receives Slack events and dispatches to agents
   - **lisa** — Agent container running Claude Code CLI

4. **Verify it's running:**

   ```bash
   # Check container status
   docker compose ps

   # Watch router logs
   docker compose logs -f router

   # Test Lisa container is responsive
   docker exec -u claude lisa claude --version
   ```

5. **Test in Slack:** Mention the bot (`@Lisa`) in a channel or send a direct message.

## Development

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run unit tests
pytest tests/unit -m unit -v

# Run integration tests
pytest tests/integration -m integration -v

# Run all tests with coverage
pytest --cov=router --cov-report=term-missing

# Lint and format check
ruff check .
ruff format --check .
```

## Architecture

```
router/          — Python router service (Slack bot + dispatcher)
packs/           — Service grants — one directory per external service (github/, zoho-mail/, …)
config/          — Per-team config: agent roles, personalities, manifests
data/            — Runtime data (secrets store; gitignored)
tests/           — Test suite (unit, integration)
docs/            — Documentation
.github/         — CI workflows
```

Each agent runs Claude Code CLI in a Docker container. The router shells `docker exec` into the agent and pipes the user's message in as context. Outside services are wired in via **packs** — self-contained directories under `packs/` that bundle a manifest, a system-prompt fragment, an optional MCP config, and an optional auth flow. Agents declare which packs they have in `config/agents/<name>/agent.yaml`. Connector-backed services (Microsoft 365, Gmail, Notion, …) are inherited from claude.ai and need no pack.

## Documentation

**Start here:**

- [Managing agents from Slack](docs/managing-agents-from-slack.md) — grant/revoke/list packs without touching the terminal.
- [Current agent roster](docs/agents.md) — who's on the team and what they do.

**Engineering runbooks:**

- [Add a new agent](docs/add-a-new-agent.md) — from Slack manifest to smoke test in under an hour.
- [Authoring a pack](docs/authoring-a-pack.md) — write a new `packs/<name>/` for a service that has no Claude connector.
- [Capabilities → packs migration](docs/capabilities-simplification.md) — the design rationale and history of the simplification.

**Reference:**

- [Scheduled tasks](docs/scheduled-tasks.md) — `/tasks` slash command, cron scheduler, seed tasks.
- [Testing guide](docs/testing.md) — how to run and structure tests.

## Autonomous bug-backlog loop

The router ships a `router.auto_dispatch` system task that autonomously drains the bug backlog at ≤ 2 fixes/hr with zero human interaction on the safe path.

**How it works:**

1. Every 30 minutes the scheduler calls `router.auto_dispatch:tick`.
2. Gate checks (all must pass): `enabled=true`, no in-flight dispatch, no open PRs, under daily cap (default 6) and hourly rate (default 2).
3. Picks the next open bug issue that has an `## Acceptance Criteria` block. Issues without an AC block are skipped.
4. Triage: evaluates blast radius via path globs. Touches auth, billing, DB migrations, deploy/compose config, or secrets → **hold for human**. Bias to hold — false-negative is dangerous. File *count* is not a gate: the merge bar is a Sam review + green CI regardless of blast radius.
5. Dispatches a `aidt-dev-worker` against the issue's AC block.
6. After the worker PR lands, reads Sam's structured verdict (`verdict: pass/fail`) and CI state.
7. `low_risk ∧ verdict=pass ∧ CI green` → auto-merge via `aidt-merge` + post "merged by bot" Slack line.
   `sensitive ∨ fail ∨ red` → hold for human (one-click merge).

**Shadow mode** (`shadow_mode=true`, the default): the loop runs, triages, dispatches, and posts verdicts, but never merges — it posts "would auto-merge" instead. Use shadow mode to validate the pipeline before going live.

**Configuration** (`config/dispatch.yaml`):

```yaml
auto_dispatch:
  enabled: false          # kill switch — flip to true to activate
  rate_per_hour: 2        # max dispatches per hour
  daily_cap: 6            # max dispatches per day (persisted across restarts)
  shadow_mode: true       # true = never merge, just log "would auto-merge"
```

**Kill switch:** set `auto_dispatch.enabled: false` (or `shadow_mode: true`). The loop goes fully inert immediately — no dispatches, no triage, a single "disabled" debug log per tick. No migration to unwind.