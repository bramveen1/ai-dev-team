# CLAUDE.md — Project Instructions

## Project Overview

Multi-agent AI dev team orchestrated via Slack. A router service receives Slack events and dispatches work to specialist agents (Lisa, etc.) running Claude Code CLI in Docker containers.

## Repository Structure

```
router/          — Python router service (Slack bot + dispatcher)
tests/           — Test suite (unit, integration, e2e)
docs/            — Documentation and spike notes
.github/         — CI workflows
```

## Development Workflow

- **Branch strategy:** Feature branches off `main`, PR-based merges
- **CI required:** All PRs must pass lint + unit tests before merge
- **Test first:** Write or update tests before or alongside implementation

## Running Tests

```bash
# Unit tests
pytest tests/unit -m unit -v

# Integration tests
pytest tests/integration -m integration -v

# All tests with coverage
pytest --cov=router --cov-report=term-missing

# Coverage HTML report
pytest --cov=router --cov-report=html
```

## Linting

```bash
ruff check .
ruff format --check .
```

## Definition of Done

A task is complete when:

1. Code is implemented and follows project conventions
2. Unit tests exist and pass for all new/changed code
3. Test coverage is at or above 90% for `router/` code
4. `ruff check .` and `ruff format --check .` pass with no errors
5. All CI checks pass (lint, unit tests, docker build)
6. Code has been reviewed via PR

## Code Style

- Python 3.11+
- Max line length: 120 characters
- Use `ruff` for linting and formatting
- Follow existing patterns in the codebase
- Type hints encouraged but not enforced yet

## Testing Conventions

- Use `pytest` with markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.e2e`
- Mock all external dependencies (Slack API, Claude Code CLI, filesystem where appropriate)
- Test fixtures live in `tests/fixtures/`
- Skeleton tests define interfaces — they skip until the module is implemented
- Use `pytest.importorskip()` for modules that don't exist yet

## Agent Workflow Rules

When working on a GitHub issue, use the `gh` CLI (authenticated via `GITHUB_TOKEN` or `GH_TOKEN` in the environment) for all GitHub interactions. Do NOT rely on GitHub MCP tools.

1. **Link branch to issue:** After creating or checking out a feature branch, link it to the GitHub issue you are working on using `gh`:
   ```bash
   # Associate the branch with the issue (creates a "linked branch" reference)
   gh issue develop <issue-number> --branch <branch-name> --base main
   # Or if the branch already exists, mention it in a comment
   gh issue comment <issue-number> --body "Working on this in branch \`<branch-name>\`"
   ```
2. **Create a PR when done:** Once implementation is complete, all tests pass, and linting is clean, automatically create a pull request using `gh pr create` — do not wait for explicit instruction.
3. **Monitor CI after PR creation:** After creating a PR, poll CI status with `gh pr checks <pr-number> --watch` or `gh run list / gh run watch` to monitor workflow runs. If CI fails, investigate the failure with `gh run view <run-id> --log-failed`, fix the issue, push the fix, and continue monitoring until CI is green.

## Key Dependencies

- `slack_bolt` — Slack bot framework
- `pytest` / `pytest-asyncio` / `pytest-cov` — Testing
- `ruff` — Linting and formatting
