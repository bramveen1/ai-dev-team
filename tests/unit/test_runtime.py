"""Unit tests for router.runtime — the shared cross-module registries.

Focused on workers_client()'s ChatAdapter routing (#841, default-on and
raw-Slack fallback deleted by #862): the workers-bot outbound factory only
ever returns a resolved ChatAdapter now — the flag off, a missing/Slack
transport (i.e. every existing no-argument call site), a missing
conversation_ref, or an unsupported/unresolvable transport all return None
instead of constructing a raw Slack AsyncWebClient.
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


# ── No Slack fallback — flag off, or missing/Slack transport, returns None ──


class TestWorkersClientNoSlackFallback:
    def test_flag_off_returns_none_regardless_of_token(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "0")

        assert runtime.workers_client() is None

    def test_flag_off_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "0")

        assert runtime.workers_client() is None

    def test_flag_on_but_no_transport_returns_none(self, monkeypatch):
        """Every current call site invokes workers_client() with no args — flag-on
        with no transport must not fall back to a raw Slack client."""
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        assert runtime.workers_client() is None

    def test_flag_on_slack_transport_returns_none(self, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-841")
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "1")

        result = runtime.workers_client(transport="slack", agent_name="sam", conversation_ref="slack:C1:1.0")

        assert result is None


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
        monkeypatch.setenv("WORKERS_CLIENT_VIA_CHAT_ADAPTER", "0")

        adapter = MagicMock(name="discord_adapter")
        adapter.agent_name = "sam"
        runtime.discord_adapters.append(adapter)

        result = runtime.workers_client(transport="discord", agent_name="sam", conversation_ref="discord:1:2:3")

        assert result is None


# ── slack_adapter_for_agent (#875) ──────────────────────────────────────────


class TestSlackAdapterForAgent:
    def test_slack_adapter_for_agent_returns_none_without_client(self, monkeypatch):
        monkeypatch.setattr(runtime, "client_for_agent", lambda agent_name: None)

        result = runtime.slack_adapter_for_agent("sam")

        assert result is None

    def test_slack_adapter_for_agent_wraps_live_bolt_client(self, monkeypatch):
        from router.chat.adapters.slack import SlackAdapter

        client = MagicMock(name="bolt_client")
        monkeypatch.setattr(runtime, "client_for_agent", lambda agent_name: client)

        result = runtime.slack_adapter_for_agent("sam")

        assert isinstance(result, SlackAdapter)
