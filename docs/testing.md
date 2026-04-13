# Testing Guide

## Quick Start

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run unit tests
pytest tests/unit -m unit -v

# Run integration tests
pytest tests/integration -m integration -v

# Run all tests
pytest -v
```

## Test Commands

### Unit Tests

```bash
pytest tests/unit -m unit -v
```

Fast tests that mock all external dependencies. These should always pass quickly and never require network access or Docker.

### Integration Tests

```bash
pytest tests/integration -m integration -v
```

Tests that verify components work together using real filesystem operations (temp directories) and fixture files. May require Docker for some tests in the future.

### Coverage

```bash
# Terminal report
pytest --cov=router --cov-report=term-missing

# HTML report (opens in browser)
pytest --cov=router --cov-report=html
open htmlcov/index.html
```

Coverage target: **90%** for `router/` code.

### Linting

```bash
# Check for lint errors
ruff check .

# Check formatting
ruff format --check .

# Auto-fix lint errors
ruff check --fix .

# Auto-format code
ruff format .
```

## Test Directory Structure

```
tests/
├── conftest.py              # Shared fixtures used across all tests
├── fixtures/                # Static test data
│   ├── memory/              # Sample MEMORY.md and agent memory files
│   │   ├── MEMORY.md
│   │   └── agents/lisa/memory.md
│   ├── systems/             # Sample systems documentation
│   │   └── outlook.md
│   ├── slack_events/        # Sample Slack webhook payloads
│   │   ├── app_mention.json
│   │   ├── direct_message.json
│   │   └── thread_reply.json
│   └── role_files/          # Sample agent role definitions
│       └── lisa_role.md
├── unit/                    # Unit tests (fast, no external deps)
│   ├── test_config.py
│   ├── test_dispatcher.py
│   ├── test_session_manager.py
│   ├── test_thread_loader.py
│   ├── test_context_builder.py
│   ├── test_memory_loader.py
│   ├── test_memory_writer.py
│   └── test_session_end.py
└── integration/             # Integration tests (may need Docker)
    ├── test_router_flow.py
    ├── test_memory_roundtrip.py
    └── test_context_assembly.py
```

## Skeleton Test Pattern

Tests are written **before** the implementation modules exist. This is intentional — the tests define the interface contract that each module must satisfy.

Each test file uses `pytest.importorskip()` at the top:

```python
config = pytest.importorskip("router.config", reason="router.config not yet implemented")
```

This means:
- If the module **doesn't exist yet**, the test is reported as **SKIPPED** (not FAILED or ERROR)
- Once the module is implemented, the tests automatically activate and verify the interface
- The test names and assertions document what the module must do

This approach ensures:
1. The test infrastructure is validated independently of implementation
2. Interface contracts are defined up front
3. Implementation PRs get immediate test coverage without needing to write tests separately

## Fixtures

### `mock_slack_client`
A `MagicMock` Slack `WebClient` with common async methods stubbed (`chat_postMessage`, `conversations_replies`, `reactions_add`).

### `mock_slack_event` / `mock_dm_event` / `mock_thread_reply_event`
Sample Slack event payloads loaded from JSON fixture files.

### `test_memory_dir`
Creates a temporary directory populated with sample memory files from `tests/fixtures/memory/`. Cleaned up automatically after the test.

### `sample_thread_history`
A list of mock thread messages for testing thread parsing.

### `sample_role_md`
Lisa's `role.md` content loaded from fixture files.

### `env_with_defaults`
Sets test environment variables (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, etc.) using `monkeypatch`.

## CI Pipeline

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs:

1. **lint** — `ruff check .` and `ruff format --check .`
2. **test-unit** — Unit tests with coverage reporting
3. **test-integration** — Integration tests (runs after unit tests pass)
4. **docker-build** — Verifies all Dockerfiles build successfully

All PRs to `main` must pass `lint` and `test-unit` before merge.
