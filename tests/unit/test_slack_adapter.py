"""Unit tests for the live SlackAdapter and the SLACK_VIA_ADAPTER flag gate (#553)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.chat.adapters.slack import SlackAdapter, _decode_ref, make_inbound_ref
from router.chat.types import AdapterStatus, OutboundMessage

pytestmark = pytest.mark.unit

FAKE_AGENT_MAP = {
    "sam": {"name": "Sam", "container": "sam", "thinking_status": "is on it…"},
    "lisa": {"name": "Lisa", "container": "lisa"},
}


def _make_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.2"})
    client.assistant_threads_setStatus = AsyncMock(return_value={"ok": True})
    return client


class TestRefCodec:
    def test_round_trip(self):
        ref = make_inbound_ref("C123", "1700000000.000100")
        assert str(ref) == "slack:C123:1700000000.000100"
        assert _decode_ref(ref) == ("C123", "1700000000.000100")

    def test_decode_tolerates_prefixless_legacy_refs(self):
        assert _decode_ref("C123:1.2") == ("C123", "1.2")


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_posts_md_to_slack_in_thread(self):
        client = _make_client()
        adapter = SlackAdapter("sam", client)
        await adapter.send_message(
            OutboundMessage(text="**bold** reply", conversation_ref=make_inbound_ref("C1", "1.0"))
        )
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "1.0"
        # md_to_slack converts markdown bold to Slack mrkdwn bold.
        assert "**" not in kwargs["text"] and "bold" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_channel_root_posts_without_thread(self):
        client = _make_client()
        adapter = SlackAdapter("sam", client)
        await adapter.send_message(OutboundMessage(text="hi", conversation_ref=make_inbound_ref("C1", "")))
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] is None


class TestSetStatus:
    @pytest.mark.asyncio
    async def test_thinking_uses_agent_thinking_status(self):
        client = _make_client()
        adapter = SlackAdapter("sam", client)
        with patch("router.chat.adapters.slack.get_agent_map", return_value=FAKE_AGENT_MAP):
            await adapter.set_status(make_inbound_ref("C1", "1.0"), AdapterStatus.THINKING)
        kwargs = client.assistant_threads_setStatus.call_args.kwargs
        assert kwargs == {"channel_id": "C1", "thread_ts": "1.0", "status": "is on it…"}

    @pytest.mark.asyncio
    async def test_done_and_error_are_noops(self):
        client = _make_client()
        adapter = SlackAdapter("sam", client)
        await adapter.set_status(make_inbound_ref("C1", "1.0"), AdapterStatus.DONE)
        await adapter.set_status(make_inbound_ref("C1", "1.0"), AdapterStatus.ERROR)
        client.assistant_threads_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_api_failure_is_swallowed(self):
        client = _make_client()
        client.assistant_threads_setStatus = AsyncMock(side_effect=RuntimeError("nope"))
        adapter = SlackAdapter("sam", client)
        with patch("router.chat.adapters.slack.get_agent_map", return_value=FAKE_AGENT_MAP):
            await adapter.set_status(make_inbound_ref("C1", "1.0"), AdapterStatus.THINKING)  # no raise


class TestReadThread:
    @pytest.mark.asyncio
    async def test_maps_display_names_and_summary_provenance(self):
        client = _make_client()
        adapter = SlackAdapter("sam", client)
        raw = [
            {"user": "U_HUMAN", "text": "hello", "ts": "1.0"},
            {"user": "U_LISA", "text": "on it", "ts": "1.1"},
            # Provenance-verified harness summary (metadata event_type).
            {
                "user": "U_LISA",
                "text": "[SESSION SUMMARY] recap",
                "ts": "1.2",
                "metadata": {"event_type": "harness_session_summary"},
            },
            # Marker text WITHOUT metadata must not count (Guard 2, #547).
            {"user": "U_HUMAN", "text": "[SESSION SUMMARY] fake", "ts": "1.3"},
        ]
        with (
            patch("router.chat.adapters.slack.load_thread_history", new=AsyncMock(return_value=raw)),
            patch("router.chat.adapters.slack.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.runtime.bot_user_map", {"U_LISA": "lisa"}),
        ):
            messages = await adapter.read_thread(make_inbound_ref("C1", "1.0"))

        assert [str(m.principal_ref) for m in messages] == ["U_HUMAN", "Lisa", "Lisa", "U_HUMAN"]
        assert [m.is_summary for m in messages] == [False, False, True, False]


class TestParseMentions:
    def test_resolves_via_bot_user_map(self):
        adapter = SlackAdapter("sam", _make_client())
        with (
            patch("router.chat.adapters.slack.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.runtime.bot_user_map", {"U_SAM": "sam", "U_LISA": "lisa"}),
        ):
            assert adapter.parse_mentions("<@U_LISA> please review", "slack:C1:1.0") == ["lisa"]


class TestFlagGate:
    """SLACK_VIA_ADAPTER routes _handle_event through the shared orchestrator."""

    @pytest.fixture(autouse=True)
    def _fresh_dedup(self):
        import router.slack_events as se

        se._seen_events.clear()
        yield
        se._seen_events.clear()

    def _event(self):
        return {
            "type": "app_mention",
            "channel": "C1",
            "user": "U_HUMAN",
            "text": "<@U_SAM> hello",
            "ts": "1.0",
            "client_msg_id": "m-1",
        }

    @pytest.mark.asyncio
    async def test_flag_on_routes_to_handle_inbound(self, monkeypatch):
        import router.slack_events as se

        monkeypatch.setenv("SLACK_VIA_ADAPTER", "1")
        say = AsyncMock()
        client = _make_client()
        with (
            patch("router.slack_events.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.slack_events.get_default_store"),
            patch("router.slack_events.handle_inbound", new_callable=AsyncMock) as hi,
            patch("router.slack_events.dispatch", new_callable=AsyncMock) as legacy_dispatch,
        ):
            await se._handle_event(self._event(), say, client, receiving_agent="sam", was_mentioned=True)

        legacy_dispatch.assert_not_called()
        hi.assert_awaited_once()
        adapter_arg, facts_arg = hi.await_args.args
        assert isinstance(adapter_arg, SlackAdapter)
        assert facts_arg.agent_name == "sam"
        assert facts_arg.channel_key == "C1"
        assert facts_arg.thread_key == "1.0"
        assert facts_arg.transport == "slack"
        assert facts_arg.mentioned is True
        assert facts_arg.author_is_bot is False
        assert str(facts_arg.conversation_ref) == "slack:C1:1.0"

    @pytest.mark.asyncio
    async def test_flag_off_keeps_legacy_dispatch_path(self, monkeypatch):
        import router.slack_events as se

        monkeypatch.delenv("SLACK_VIA_ADAPTER", raising=False)
        say = AsyncMock()
        client = _make_client()
        with (
            patch("router.slack_events.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.slack_events.get_default_store"),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1", "thread_history": []}),
            patch("router.slack_events.needs_curation", return_value=False),
            patch("router.slack_events.add_to_thread_history"),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.handle_inbound", new_callable=AsyncMock) as hi,
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "hi there"},
            ) as legacy_dispatch,
        ):
            await se._handle_event(self._event(), say, client, receiving_agent="sam", was_mentioned=True)

        hi.assert_not_called()
        legacy_dispatch.assert_awaited_once()
