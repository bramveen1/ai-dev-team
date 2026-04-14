"""Shared test fixtures for the ai-dev-team test suite."""

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    """Return the path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def mock_slack_client():
    """Return a mock Slack WebClient with common methods stubbed."""
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1705700000.000100"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.conversations_replies = AsyncMock(
        return_value={
            "ok": True,
            "messages": [
                {"user": "U0001", "text": "Hey Lisa, can you help?", "ts": "1705700000.000100"},
                {"user": "U_BOT", "text": "Sure, looking into it now.", "ts": "1705700001.000200"},
            ],
        }
    )
    client.reactions_add = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture
def mock_slack_event():
    """Return a sample app_mention event dict."""
    event_path = FIXTURES_DIR / "slack_events" / "app_mention.json"
    with open(event_path) as f:
        return json.load(f)


@pytest.fixture
def mock_dm_event():
    """Return a sample direct message event dict."""
    event_path = FIXTURES_DIR / "slack_events" / "direct_message.json"
    with open(event_path) as f:
        return json.load(f)


@pytest.fixture
def mock_thread_reply_event():
    """Return a sample thread reply event dict."""
    event_path = FIXTURES_DIR / "slack_events" / "thread_reply.json"
    with open(event_path) as f:
        return json.load(f)


@pytest.fixture
def test_memory_dir():
    """Create a temp directory with sample memory files, yield path, then clean up."""
    tmpdir = tempfile.mkdtemp(prefix="ai_dev_team_test_")
    memory_src = FIXTURES_DIR / "memory"

    # Copy fixture memory files into temp dir
    for src_file in memory_src.rglob("*"):
        if src_file.is_file():
            rel = src_file.relative_to(memory_src)
            dest = Path(tmpdir) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)

    yield Path(tmpdir)

    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def sample_thread_history():
    """Return a list of mock thread messages."""
    return [
        {"user": "U0001", "text": "Hey Lisa, can you review this PR?", "ts": "1705700000.000100"},
        {"user": "U_BOT", "text": "Sure, I'll take a look at the changes.", "ts": "1705700010.000200"},
        {"user": "U0001", "text": "Focus on the auth module please.", "ts": "1705700020.000300"},
        {"user": "U_BOT", "text": "The auth module looks good. Add rate limiting.", "ts": "1705700030.000400"},
    ]


@pytest.fixture
def sample_role_md():
    """Return Lisa's role.md content as a string."""
    role_path = FIXTURES_DIR / "role_files" / "lisa_role.md"
    return role_path.read_text()


@pytest.fixture
def env_with_defaults(monkeypatch):
    """Set up environment variables with test defaults."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test-token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("SESSION_TIMEOUT", "300")
    monkeypatch.setenv("MAX_TOKEN_BUDGET", "4000")
