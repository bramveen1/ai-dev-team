"""Shared fixtures for dispatch pack unit tests."""

import pytest


@pytest.fixture(autouse=True)
def workers_bot_token(monkeypatch):
    """Provide WORKERS_BOT_TOKEN so dispatch_issue fail-fast does not fire in tests
    that are not specifically testing the missing-token path."""
    monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-test-workers")
