"""Unit tests for router.auto_dispatch.notify — ChatAdapter routing (#837, #858).

Mirrors ``tests/unit/dispatch/test_feed_transport.py`` (#713) / the
``TestPostInThreadChatAdapterRouting`` class in ``tests/unit/test_kill_command.py``
(#834): all three wrappers must behave identically under the same transport
matrix. #858 retired the module's rollout flag (``AUTO_DISPATCH_NOTIFY_VIA_CHAT_ADAPTER``
in ``router/settings.py`` is now default-on and unconditional; this module no
longer reads it), so the matrix below is no longer parameterized by flag state.

- Slack/unset transport → identical slack_post.best_effort_post call, always
  (no ChatAdapter ts equivalent for the auto-dispatch kickoff-thread anchor).
- Missing conversation_id → log-and-skip, no post at all.
- Unsupported transport → log-and-skip, never a silent Slack fallback.
- No adapter resolvable for the agent → log-and-skip.
- Resolvable Discord adapter → posts via adapter.send_message, never touches
  slack_client, and returns conversation_id as the ts/ref handle.
- Adapter send failure never raises (best-effort contract) and returns "".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.auto_dispatch import notify

pytestmark = pytest.mark.unit


def _slack_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1234.5678"})
    return client


@pytest.mark.asyncio
class TestSlackOrUnsetTransport:
    async def test_slack_transport_posts_via_slack(self):
        client = _slack_client()
        ts = await notify._slack_post_with_ts(client, "C1", "hello", agent="sam", transport="slack")
        assert ts == "1234.5678"
        client.chat_postMessage.assert_awaited_once()

    async def test_unset_transport_posts_via_slack(self):
        client = _slack_client()
        await notify._slack_post(client, "C1", "hello", agent="sam")
        client.chat_postMessage.assert_awaited_once()

    async def test_slack_post_with_ts_returns_slack_ts(self):
        client = _slack_client()
        ts = await notify._slack_post_with_ts(client, "C1", "hello", agent="sam")
        assert ts == "1234.5678"
        client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
class TestDegradeModes:
    async def test_missing_conversation_id_skips_post_entirely(self):
        client = _slack_client()
        ts = await notify._slack_post_with_ts(client, "", "hello", agent="sam", transport="discord")
        assert ts == ""
        client.chat_postMessage.assert_not_awaited()

    async def test_unsupported_transport_skips_without_slack_fallback(self):
        client = _slack_client()
        ts = await notify._slack_post_with_ts(
            client, "C1", "hello", agent="sam", transport="teams", conversation_id="teams:abc"
        )
        assert ts == ""
        client.chat_postMessage.assert_not_awaited()

    async def test_no_adapter_for_agent_skips_post(self, monkeypatch):
        monkeypatch.setattr(notify.runtime, "discord_adapter_for_agent", lambda agent: None)
        client = _slack_client()
        ts = await notify._slack_post_with_ts(
            client, "C1", "hello", agent="sam", transport="discord", conversation_id="discord:1:2:3"
        )
        assert ts == ""
        client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
class TestAdapterRouting:
    async def test_resolvable_adapter_posts_via_adapter_not_slack(self, monkeypatch):
        adapter = MagicMock()
        adapter.send_message = AsyncMock()
        monkeypatch.setattr(notify.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        client = _slack_client()
        ts = await notify._slack_post_with_ts(
            client, "", "hello", agent="sam", transport="discord", conversation_id="discord:1:2:3"
        )

        adapter.send_message.assert_awaited_once()
        outbound = adapter.send_message.await_args.args[0]
        assert outbound.text == "hello"
        assert str(outbound.conversation_ref) == "discord:1:2:3"
        client.chat_postMessage.assert_not_awaited()
        # Adapter has no ts concept — conversation_id is the surfaced handle.
        assert ts == "discord:1:2:3"

    async def test_slack_post_variant_also_routes_via_adapter(self, monkeypatch):
        adapter = MagicMock()
        adapter.send_message = AsyncMock()
        monkeypatch.setattr(notify.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        client = _slack_client()
        await notify._slack_post(client, "", "hello", agent="sam", transport="discord", conversation_id="discord:1:2:3")

        adapter.send_message.assert_awaited_once()
        client.chat_postMessage.assert_not_awaited()

    async def test_adapter_send_failure_never_raises(self, monkeypatch):
        adapter = MagicMock()
        adapter.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(notify.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        ts = await notify._slack_post_with_ts(
            _slack_client(), "", "hello", agent="sam", transport="discord", conversation_id="discord:1:2:3"
        )
        adapter.send_message.assert_awaited_once()
        assert ts == ""
