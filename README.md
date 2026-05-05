# ai-dev-team

Multi-agent AI dev team orchestrated via Slack. A router service receives Slack events and dispatches work to specialist agents (Lisa, etc.) running Claude Code CLI in Docker containers.

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

2. **Set up Claude Code authentication** (choose one):

   - **API key:** Add `ANTHROPIC_API_KEY=sk-ant-api03-...` to your `.env`
   - **Max subscription:** After starting the containers, run:
     ```bash
     docker exec -it lisa claude auth login --claudeai
     ```
     Credentials persist in the `lisa-claude-config` Docker volume.

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