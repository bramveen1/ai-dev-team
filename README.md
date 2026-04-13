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
agents/lisa/     — Lisa agent container (Claude Code CLI)
memory/          — Shared organizational memory
tests/           — Test suite (unit, integration, e2e)
docs/            — Documentation and spike notes
.github/         — CI workflows
```