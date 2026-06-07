"""Shared fixtures for integration tests.

Mirrors ``tests/unit/packs/conftest.py``: the dispatch handler's
``WORKERS_BOT_TOKEN`` fail-fast (PR #257 / issue #251) trips whenever a test
imports ``packs.dispatch.handler`` and exercises ``dispatch_issue`` without
the token in the environment. Integration smoke tests don't ride on the unit
conftest, so they need their own autouse fixture.
"""

import pytest


@pytest.fixture(autouse=True)
def workers_bot_token(monkeypatch):
    """Provide WORKERS_BOT_TOKEN so dispatch_issue fail-fast does not fire in
    integration tests that are not specifically testing the missing-token path.
    """
    monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-test-workers-integration")
