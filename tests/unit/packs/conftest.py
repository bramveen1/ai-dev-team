"""Shared fixtures for dispatch pack unit tests."""

import urllib.request

import pytest


@pytest.fixture(autouse=True)
def workers_bot_token(monkeypatch):
    """Provide WORKERS_BOT_TOKEN so dispatch_issue fail-fast does not fire in tests
    that are not specifically testing the missing-token path."""
    monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-test-workers")


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Make dispatch-pack unit tests hermetic: never egress to the real network.

    ``dispatch_issue`` posts Slack "started"/"queued" messages via
    ``_post_slack_message``, which falls back to the real
    ``urllib.request.urlopen`` whenever a test does not inject a fake
    ``_urlopen`` seam. The slot-pool / dispatch-flow tests drive
    ``dispatch_issue`` with a non-empty channel and a (fake) WORKERS_BOT_TOKEN,
    so each one fired live HTTPS requests to ``slack.com/api/chat.postMessage``.
    They only "passed" because the network error was swallowed — making the
    result depend on the runner's network reachability/latency. Under a slow or
    blocked CI runner the stacked 5s-timeout calls pushed the job over its limit
    and it died mid-test, surfacing as a flaky ``test_three_dispatches_*``
    failure (see #466).

    Patching ``urllib.request.urlopen`` to an in-memory no-op kills only the
    egress: tests that exercise Slack logic directly inject their own
    ``_urlopen`` (or patch their handler's ``urlopen``) and are unaffected;
    everything else becomes deterministic and offline. Returning ``None`` is the
    same benign "post succeeded" signal the in-file fakes already use, and it
    cannot leak through ``_post_slack_message`` (which only catches
    URLError/OSError/RuntimeError) the way a raising stub would.
    """

    def _no_network(req, timeout=None):  # noqa: ARG001 - signature mirrors urlopen
        return None

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
