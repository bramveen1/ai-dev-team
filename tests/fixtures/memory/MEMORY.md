# Team Memory

## Architecture Decisions

- **2024-01-15**: Chose `slack_bolt` for Slack integration — async-capable, well-documented, official SDK.
- **2024-01-16**: Router dispatches to agents via Claude Code CLI in Docker containers.
- **2024-01-17**: Each agent gets its own memory file under `agents/<name>/memory.md`.

## Active Conventions

- All Slack messages processed through the router service
- Agents communicate results back via Slack thread replies
- Memory files are markdown format, append-only during sessions

## Known Issues

- Token budget estimation is approximate — revisit after initial testing
- Session timeout set to 5 minutes — may need tuning
