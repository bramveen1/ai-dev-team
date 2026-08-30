"""Unit tests for router.runtime — the shared cross-module registries.

Focused on workers_client()'s ChatAdapter routing (#841): the workers-bot
outbound factory gains an opt-in adapter path behind a default-off flag while
every existing (no-argument) call site keeps getting the legacy raw Slack
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


# ── Legacy (flag-off / no-argument) behaviour — must stay byte-identical ────


class TestWorkersClientLegacyPath:
    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        monkeypatch.delenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", raising=False)

        assert runtime.workers_client() is None

    def test_token_set_returns_async_web_client(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.delenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", raising=False)

        client = runtime.workers_client()

        assert client is not None
        assert client.token == "xoxb-workers-841"

    def test_flag_on_but_no_transport_uses_legacy_path(self, monkeypatch):
        """Every current call site invokes workers_client() with no args — flag-on
        must not change their behaviour."""
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        client = runtime.workers_client()

        assert client is not None
        assert client.token == "xoxb-workers-841"

    def test_flag_on_slack_transport_uses_legacy_path(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        client = runtime.workers_client(transport="slack", agent_name="sam", conversation_ref="slack:C1:1.0")

        assert client is not None
        assert client.token == "xoxb-workers-841"


# ── Flag-on adapter path ────────────────────────────────────────────────────


class TestWorkersClientChatAdapterRouting:
    def test_missing_conversation_ref_skips_without_slack_fallback(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref=None)

        assert result is None

    def test_unsupported_transport_skips_without_slack_fallback(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        result = runtime.workers_client(transport="teams", agent_name="sam", conversation_ref="teams:abc")

        assert result is None

    def test_no_adapter_for_agent_returns_none(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is None

    def test_resolvable_adapter_returned_instead_of_slack_client(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        adapter = MagicMock(name="discord_adapter")
        adapter.agent_name = "sam"
        runtime.discord_adapters.append(adapter)

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is adapter

    def test_flag_off_never_consults_discord_adapters(self, monkeypatch):
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        monkeypatch.delenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", raising=False)

        adapter = MagicMock(name="discord_adapter")
        adapter.agent_name = "sam"
        runtime.discord_adapters.append(adapter)

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is None
