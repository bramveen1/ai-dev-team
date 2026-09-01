"""Unit tests for router.runtime — the shared cross-module registries.

Focused on workers_client()'s ChatAdapter routing (#841, finalized default-on
by #862 — the former WORKERS_CLIENT_VIA_CHAT_ADAPTER rollout flag in
router/settings.py is now on unconditionally; this module no longer reads
it): a resolvable non-Slack transport always prefers the adapter, and every
existing (no-argument) call site keeps getting the legacy raw Slack
AsyncWebClient, byte-for-byte.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import router.runtime as runtime

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_discord_adapters():
    runtime.discord_adapters.clear()
    yield
    runtime.discord_adapters.clear()


# ── Legacy (no-argument / Slack) behaviour — must stay byte-identical ───────


class TestWorkersClientLegacyPath:
    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)

        assert runtime.workers_client() is None

    def test_token_set_returns_async_web_client(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")

        client = runtime.workers_client()

        assert client is not None
        assert client.token == "xoxb-workers-841"

    def test_slack_transport_uses_legacy_path(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")

        client = runtime.workers_client(transport="slack", agent_name="sam", conversation_ref="slack:C1:1.0")

        assert client is not None
        assert client.token == "xoxb-workers-841"


# ── ChatAdapter routing ──────────────────────────────────────────────────────


class TestWorkersClientChatAdapterRouting:
    def test_missing_conversation_ref_skips_without_slack_fallback(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref=None)

        assert result is None

    def test_unsupported_transport_skips_without_slack_fallback(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")

        result = runtime.workers_client(transport="teams", agent_name="sam", conversation_ref="teams:abc")

        assert result is None

    def test_no_adapter_for_agent_returns_none(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is None

    def test_resolvable_adapter_returned_instead_of_slack_client(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")

        adapter = MagicMock(name="discord_adapter")
        adapter.agent_name = "sam"
        runtime.discord_adapters.append(adapter)

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is adapter

    def test_no_token_adapter_still_resolves(self, monkeypatch):
        """The adapter path never depends on WORKERS_BOT_TOKEN — it's a Slack-only
        legacy concern — so an unconfigured workers app still routes correctly."""
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)

        adapter = MagicMock(name="discord_adapter")
        adapter.agent_name = "sam"
        runtime.discord_adapters.append(adapter)

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is adapter
