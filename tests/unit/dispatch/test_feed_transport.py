"""Unit tests for router.dispatch.feed_transport (#713).

Covers the single choke point milestone_feed/supervision route through:
- Flag off → identical slack_post.best_effort_post call, always.
- Flag on + Slack/unset transport → identical slack_post call (no-op flag).
- Flag on + missing conversation_id → log-and-skip, no post at all.
- Flag on + unsupported transport → log-and-skip, never a silent Slack fallback.
- Flag on + no adapter resolvable for the agent → log-and-skip.
- Flag on + resolvable Discord adapter → posts via adapter.send_message,
  never touches slack_client.
- Adapter send failure never raises (best-effort contract).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import feed_transport

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.feed_transport")


def _slack_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


@pytest.mark.asyncio
class TestFlagOff:
    async def test_flag_off_posts_via_slack_regardless_of_transport(self, monkeypatch):
        monkeypatch.delenv(feed_transport.ENV_FLAG, raising=False)
        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            text="hello",
            agent="sam",
            transport="discord",
            conversation_id="discord:1:2:3",
            log=_LOG,
            prefix="test",
        )
        client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
class TestFlagOnSlackOrUnsetTransport:
    async def test_slack_transport_posts_via_slack(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            text="hello",
            agent="sam",
            transport="slack",
            conversation_id="",
            log=_LOG,
            prefix="test",
        )
        client.chat_postMessage.assert_awaited_once()

    async def test_unset_transport_posts_via_slack(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            text="hello",
            agent="sam",
            transport="",
            conversation_id="",
            log=_LOG,
            prefix="test",
        )
        client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
class TestFlagOnDegradeModes:
    async def test_missing_conversation_id_skips_post_entirely(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="",
            thread_ts="",
            text="hello",
            agent="sam",
            transport="discord",
            conversation_id="",
            log=_LOG,
            prefix="test",
        )
        client.chat_postMessage.assert_not_awaited()

    async def test_unsupported_transport_skips_without_slack_fallback(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            text="hello",
            agent="sam",
            transport="teams",
            conversation_id="teams:abc",
            log=_LOG,
            prefix="test",
        )
        # No silent cross-transport fallback — the Slack client must never
        # see this post even though channel/thread_ts are populated.
        client.chat_postMessage.assert_not_awaited()

    async def test_no_adapter_for_agent_skips_post(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: None)
        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            text="hello",
            agent="sam",
            transport="discord",
            conversation_id="discord:1:2:3",
            log=_LOG,
            prefix="test",
        )
        client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
class TestFlagOnAdapterRouting:
    async def test_resolvable_adapter_posts_via_adapter_not_slack(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        adapter = MagicMock()
        adapter.send_message = AsyncMock()
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        client = _slack_client()
        await feed_transport.post(
            slack_client=client,
            channel="",
            thread_ts="",
            text="hello",
            agent="sam",
            transport="discord",
            conversation_id="discord:1:2:3",
            log=_LOG,
            prefix="test",
        )

        adapter.send_message.assert_awaited_once()
        outbound = adapter.send_message.await_args.args[0]
        assert outbound.text == "hello"
        assert str(outbound.conversation_ref) == "discord:1:2:3"
        client.chat_postMessage.assert_not_awaited()

    async def test_adapter_send_failure_never_raises(self, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        adapter = MagicMock()
        adapter.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        await feed_transport.post(
            slack_client=_slack_client(),
            channel="",
            thread_ts="",
            text="hello",
            agent="sam",
            transport="discord",
            conversation_id="discord:1:2:3",
            log=_LOG,
            prefix="test",
        )
        adapter.send_message.assert_awaited_once()
