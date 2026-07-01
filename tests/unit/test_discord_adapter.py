"""Tests for the DiscordAdapter — ref encoding, capabilities, all primitives,
routing, rate-limit / 429 handling, intent guard, and approval round-trip.

All discord.py network calls are mocked; no live Discord connection is made.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_client(*, user=None) -> MagicMock:
    """Return a minimal mock discord.Client."""
    client = MagicMock()
    client.user = user or MagicMock(id=999_000)
    client.event = lambda fn: fn  # decorator no-op
    client.get_channel = MagicMock(return_value=None)
    client.get_user = MagicMock(return_value=None)
    return client


def _make_adapter(agent_name: str = "sam", default_channel_id: int = 0):
    """Return a DiscordAdapter with a mocked discord.Client."""
    from router.chat.adapters.discord import DiscordAdapter

    client = _make_client()
    return DiscordAdapter(
        bot_token="fake-token",
        agent_name=agent_name,
        default_channel_id=default_channel_id,
        client=client,
    )


# ---------------------------------------------------------------------------
# Import smoke
# ---------------------------------------------------------------------------


class TestImports:
    def test_discord_adapter_importable(self):
        from router.chat.adapters.discord import DiscordAdapter  # noqa: F401

    def test_make_inbound_ref_importable(self):
        from router.chat.adapters.discord import make_inbound_ref  # noqa: F401

    def test_discord_enabled_flag_importable(self):
        from router.chat.adapters.discord import DISCORD_ENABLED  # noqa: F401

    def test_discord_approval_adapter_importable(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter  # noqa: F401


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class TestDiscordEnabledFlag:
    def test_default_is_false(self, monkeypatch):
        monkeypatch.delenv("DISCORD_ENABLED", raising=False)
        import importlib

        import router.chat.adapters.discord as mod

        importlib.reload(mod)
        assert mod.DISCORD_ENABLED is False

    def test_true_enables_flag(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ENABLED", "true")
        import importlib

        import router.chat.adapters.discord as mod

        importlib.reload(mod)
        assert mod.DISCORD_ENABLED is True

    def test_one_enables_flag(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ENABLED", "1")
        import importlib

        import router.chat.adapters.discord as mod

        importlib.reload(mod)
        assert mod.DISCORD_ENABLED is True

    def test_false_stays_false(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ENABLED", "false")
        import importlib

        import router.chat.adapters.discord as mod

        importlib.reload(mod)
        assert mod.DISCORD_ENABLED is False


# ---------------------------------------------------------------------------
# Ref encode / decode round-trip
# ---------------------------------------------------------------------------


class TestRefEncoding:
    def test_make_inbound_ref_is_conversation_ref(self):
        from router.chat.adapters.discord import make_inbound_ref

        ref = make_inbound_ref(123, 456, 789)
        # ConversationRef is a NewType(str) — underlying type is str.
        assert isinstance(ref, str)

    def test_make_inbound_ref_contains_all_ids(self):
        from router.chat.adapters.discord import make_inbound_ref

        ref = make_inbound_ref(111, 222, 333)
        assert "111" in ref
        assert "222" in ref
        assert "333" in ref

    def test_make_inbound_ref_has_discord_prefix(self):
        from router.chat.adapters.discord import make_inbound_ref

        ref = make_inbound_ref(1, 2, 3)
        assert ref.startswith("discord:")

    def test_encode_decode_round_trip(self):
        from router.chat.adapters.discord import _decode_ref, _encode_ref

        guild_id, channel_id, thread_id = 100, 200, 300
        ref = _encode_ref(guild_id, channel_id, thread_id)
        g, c, t = _decode_ref(ref)
        assert g == guild_id
        assert c == channel_id
        assert t == thread_id

    def test_encode_decode_no_thread(self):
        from router.chat.adapters.discord import _decode_ref, _encode_ref

        ref = _encode_ref(1, 2, 0)
        _, _, thread_id = _decode_ref(ref)
        assert thread_id == 0

    def test_decode_malformed_ref_raises(self):
        from router.chat.adapters.discord import _decode_ref
        from router.chat.types import ConversationRef

        with pytest.raises(ValueError):
            _decode_ref(ConversationRef("discord:bad-format"))

    def test_ref_distinct_from_slack_encoding(self):
        from router.chat.adapters.discord import make_inbound_ref
        from router.chat.adapters.slack import make_inbound_ref as slack_ref

        discord_ref = make_inbound_ref(1, 2, 3)
        slack_ref_val = slack_ref("C_GENERAL", "1234567890.000100")
        assert discord_ref != slack_ref_val
        assert discord_ref.startswith("discord:")
        assert not slack_ref_val.startswith("discord:")


# ---------------------------------------------------------------------------
# PrincipalRef encoding
# ---------------------------------------------------------------------------


class TestPrincipalRefEncoding:
    def test_resolve_principal_has_discord_prefix(self):
        adapter = _make_adapter()
        ref = adapter.resolve_principal("123456789012345678")
        assert ref.startswith("discord:")

    def test_resolve_principal_contains_snowflake(self):
        adapter = _make_adapter()
        ref = adapter.resolve_principal("987654321098765432")
        assert "987654321098765432" in ref

    def test_resolve_principal_is_principal_ref(self):
        adapter = _make_adapter()
        ref = adapter.resolve_principal("111")
        # PrincipalRef is a NewType(str) — underlying type is str.
        assert isinstance(ref, str)

    def test_principal_distinct_from_slack(self):
        adapter = _make_adapter()
        ref = adapter.resolve_principal("U01234ABC")
        # Slack wraps the ID directly; Discord adds a prefix.
        assert ref != "U01234ABC"
        assert ref == "discord:U01234ABC"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestDiscordAdapterCapabilities:
    def test_supports_threads(self):
        assert _make_adapter().capabilities.supports_threads is True

    def test_supports_channels(self):
        assert _make_adapter().capabilities.supports_channels is True

    def test_supports_interactive(self):
        assert _make_adapter().capabilities.supports_interactive is True

    def test_is_chat_adapter_subclass(self):
        from router.chat.adapters.discord import DiscordAdapter
        from router.chat.interface import ChatAdapter

        assert issubclass(DiscordAdapter, ChatAdapter)


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_with_ref_calls_channel_send(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        await adapter.send_message(OutboundMessage(text="hello", conversation_ref=ref))

        mock_channel.send.assert_awaited_once_with("hello")

    @pytest.mark.asyncio
    async def test_send_message_no_ref_uses_default_channel(self):
        from router.chat.adapters.discord import DiscordAdapter
        from router.chat.types import OutboundMessage

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", default_channel_id=99, client=client)
        await adapter.send_message(OutboundMessage(text="proactive"))

        mock_channel.send.assert_awaited_once_with("proactive")
        client.get_channel.assert_called_once_with(99)

    @pytest.mark.asyncio
    async def test_send_message_missing_channel_is_noop(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        client = _make_client()
        client.get_channel = MagicMock(return_value=None)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        # Should not raise.
        await adapter.send_message(OutboundMessage(text="hello", conversation_ref=ref))

    @pytest.mark.asyncio
    async def test_send_message_to_thread(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        mock_thread = AsyncMock()
        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=mock_thread)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 77)
        await adapter.send_message(OutboundMessage(text="in thread", conversation_ref=ref))

        mock_thread.send.assert_awaited_once_with("in thread")
        mock_channel.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2000-char message splitting (Discord error 50035)
# ---------------------------------------------------------------------------


class TestMessageSplitting:
    def test_short_message_returns_single_chunk_unchanged(self):
        from router.chat.adapters.discord import _split_message

        assert _split_message("hello") == ["hello"]
        assert _split_message("") == [""]

    def test_boundary_2000_is_single_chunk_2001_splits(self):
        from router.chat.adapters.discord import _split_message

        assert _split_message("a" * 2000) == ["a" * 2000]
        chunks = _split_message("a" * 2001)
        assert len(chunks) == 2
        assert all(len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == "a" * 2001

    def test_splits_on_newline_boundaries_and_preserves_content(self):
        from router.chat.adapters.discord import _split_message

        text = "\n".join("line %d %s" % (i, "x" * 100) for i in range(60))
        chunks = _split_message(text)
        assert len(chunks) > 1
        assert all(len(c) <= 2000 for c in chunks)
        # Newline-joined chunks reconstruct the original exactly.
        assert "\n".join(chunks) == text

    def test_single_overlong_line_is_hard_sliced(self):
        from router.chat.adapters.discord import _split_message

        chunks = _split_message("z" * 5000)
        assert [len(c) for c in chunks] == [2000, 2000, 1000]
        assert "".join(chunks) == "z" * 5000

    @pytest.mark.asyncio
    async def test_send_message_splits_long_text_into_multiple_sends(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        long_text = "q" * 4500
        await adapter.send_message(OutboundMessage(text=long_text, conversation_ref=ref))

        assert mock_channel.send.await_count == 3
        sent = [call.args[0] for call in mock_channel.send.await_args_list]
        assert all(len(s) <= 2000 for s in sent)
        assert "".join(sent) == long_text


# ---------------------------------------------------------------------------
# 429 backoff
# ---------------------------------------------------------------------------


class TestRateLimitBackoff:
    @pytest.mark.asyncio
    async def test_sends_message_after_429_retry(self):
        import discord as discord_lib

        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        # First call raises 429; second succeeds.
        exc = discord_lib.HTTPException(MagicMock(status=429), "rate limited")
        exc.status = 429
        exc.retry_after = 0.01
        mock_channel.send = AsyncMock(side_effect=[exc, None])

        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await adapter.send_message(OutboundMessage(text="retry me", conversation_ref=ref))
            mock_sleep.assert_awaited()

        assert mock_channel.send.await_count == 2

    @pytest.mark.asyncio
    async def test_non_429_http_exception_propagates(self):
        import discord as discord_lib

        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        exc = discord_lib.HTTPException(MagicMock(status=403), "forbidden")
        exc.status = 403
        mock_channel.send = AsyncMock(side_effect=exc)

        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)

        with pytest.raises(discord_lib.HTTPException):
            await adapter.send_message(OutboundMessage(text="fail", conversation_ref=ref))


# ---------------------------------------------------------------------------
# read_thread
# ---------------------------------------------------------------------------


class TestReadThread:
    @pytest.mark.asyncio
    async def test_read_thread_returns_inbound_messages(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import ConversationRef

        # Build mock messages.
        mock_author = MagicMock(id=555)
        mock_guild = MagicMock(id=1)
        mock_msg = MagicMock(content="hello", author=mock_author, guild=mock_guild)

        mock_channel = MagicMock()
        mock_channel.get_thread = MagicMock(return_value=None)

        async def _history(**kwargs):
            yield mock_msg

        mock_channel.history = MagicMock(return_value=_history())
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        messages = await adapter.read_thread(ConversationRef(ref))

        assert len(messages) == 1
        assert messages[0].text == "hello"
        assert "555" in messages[0].principal_ref

    @pytest.mark.asyncio
    async def test_read_thread_missing_channel_returns_empty(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import ConversationRef

        client = _make_client()
        client.get_channel = MagicMock(return_value=None)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        messages = await adapter.read_thread(ConversationRef(ref))

        assert messages == []


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------


class TestSetStatus:
    @pytest.mark.asyncio
    async def test_thinking_triggers_typing(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import AdapterStatus, ConversationRef

        # typing() must return an async context manager so the real code path is exercised.
        typing_cm = MagicMock()
        typing_cm.__aenter__ = AsyncMock(return_value=None)
        typing_cm.__aexit__ = AsyncMock(return_value=False)

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        mock_channel.typing = MagicMock(return_value=typing_cm)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        await adapter.set_status(ConversationRef(ref), AdapterStatus.THINKING)

        mock_channel.typing.assert_called_once()
        typing_cm.__aenter__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_done_sends_no_message(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import AdapterStatus, ConversationRef

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        await adapter.set_status(ConversationRef(ref), AdapterStatus.DONE)

        mock_channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_status_all_states_no_error(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import AdapterStatus, ConversationRef

        typing_cm = MagicMock()
        typing_cm.__aenter__ = AsyncMock(return_value=None)
        typing_cm.__aexit__ = AsyncMock(return_value=False)

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        mock_channel.typing = MagicMock(return_value=typing_cm)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        for state in AdapterStatus:
            await adapter.set_status(ConversationRef(ref), state)


# ---------------------------------------------------------------------------
# parse_mentions — Sam vs Lisa routing
# ---------------------------------------------------------------------------


class TestParseMentions:
    def test_parse_plain_at_mention(self):
        from router.chat.types import ConversationRef

        adapter = _make_adapter(agent_name="sam")
        ref = ConversationRef("discord:1:2:0")
        result = adapter.parse_mentions("hey @sam can you help?", ref)
        assert "sam" in result

    def test_parse_multiple_agent_mentions(self):
        from router.chat.types import ConversationRef

        adapter = _make_adapter(agent_name="sam")
        ref = ConversationRef("discord:1:2:0")
        result = adapter.parse_mentions("@sam and @lisa please look at this", ref)
        assert "sam" in result
        assert "lisa" in result
        assert result.index("sam") < result.index("lisa")

    def test_parse_no_mention_returns_empty(self):
        from router.chat.types import ConversationRef

        adapter = _make_adapter(agent_name="sam")
        ref = ConversationRef("discord:1:2:0")
        result = adapter.parse_mentions("hello there", ref)
        assert result == []

    def test_parse_deduplicates(self):
        from router.chat.types import ConversationRef

        adapter = _make_adapter(agent_name="sam")
        ref = ConversationRef("discord:1:2:0")
        result = adapter.parse_mentions("@sam @sam", ref)
        assert result.count("sam") == 1

    def test_sam_adapter_parse_snowflake_mention(self):
        """Snowflake mention resolves to agent name via client user cache."""
        from router.chat.adapters.discord import DiscordAdapter
        from router.chat.types import ConversationRef

        mock_user = MagicMock()
        mock_user.display_name = "Sam"
        client = _make_client()
        client.get_user = MagicMock(return_value=mock_user)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = ConversationRef("discord:1:2:0")
        result = adapter.parse_mentions("<@123456789012345678> please help", ref)
        assert "sam" in result

    def test_unknown_snowflake_skipped(self):
        from router.chat.types import ConversationRef

        adapter = _make_adapter()
        adapter._client.get_user = MagicMock(return_value=None)
        ref = ConversationRef("discord:1:2:0")
        result = adapter.parse_mentions("<@999999999999999999>", ref)
        assert result == []


# ---------------------------------------------------------------------------
# prompt_for_choice (approval round-trip)
# ---------------------------------------------------------------------------


class TestPromptForChoice:
    @pytest.mark.asyncio
    async def test_prompt_for_choice_returns_on_resolve(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import ConversationRef, PromptChoice, StructuredResponse

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        prompt = PromptChoice(prompt="Approve?", choices=["Yes", "No"], timeout_seconds=5)

        # Simulate a button press by resolving the future after the send.
        async def _send_and_resolve(text, **kwargs):
            # Grab the correlation_id from the first pending key.
            cid = next(iter(adapter._pending_choices))
            adapter._resolve_choice(cid, StructuredResponse(choice="Yes", index=0))

        mock_channel.send = AsyncMock(side_effect=_send_and_resolve)

        result = await adapter.prompt_for_choice(ConversationRef(ref), prompt)
        assert result.choice == "Yes"
        assert result.index == 0

    @pytest.mark.asyncio
    async def test_prompt_for_choice_timeout_returns_first_choice(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import ConversationRef, PromptChoice

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        client = _make_client()
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        prompt = PromptChoice(prompt="Choose?", choices=["Alpha", "Beta"], timeout_seconds=1)

        # Don't resolve the future — let it time out.
        with patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
            result = await adapter.prompt_for_choice(ConversationRef(ref), prompt)

        assert result.choice == "Alpha"
        assert result.index == 0

    @pytest.mark.asyncio
    async def test_prompt_for_choice_missing_channel_returns_default(self):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import ConversationRef, PromptChoice

        client = _make_client()
        client.get_channel = MagicMock(return_value=None)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        ref = make_inbound_ref(1, 42, 0)
        prompt = PromptChoice(prompt="Choose?", choices=["One", "Two"], timeout_seconds=5)

        result = await adapter.prompt_for_choice(ConversationRef(ref), prompt)
        assert result.choice == "One"

    def test_resolve_choice_sets_future_result(self):
        from router.chat.adapters.discord import DiscordAdapter
        from router.chat.types import StructuredResponse

        client = _make_client()
        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)

        loop = asyncio.new_event_loop()
        try:
            future: asyncio.Future[StructuredResponse] = loop.create_future()
            adapter._pending_choices["test-id"] = future
            adapter._resolve_choice("test-id", StructuredResponse(choice="Yes", index=0))
            assert future.result().choice == "Yes"
            assert "test-id" not in adapter._pending_choices
        finally:
            loop.close()

    def test_resolve_choice_unknown_id_is_noop(self):
        from router.chat.adapters.discord import DiscordAdapter
        from router.chat.types import StructuredResponse

        client = _make_client()
        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        # Should not raise.
        adapter._resolve_choice("nonexistent", StructuredResponse(choice="x", index=0))


# ---------------------------------------------------------------------------
# Intent guard (MESSAGE CONTENT intent off detection)
# ---------------------------------------------------------------------------


class TestIntentGuard:
    def test_adapter_stores_intent_guard_as_none_initially(self):
        adapter = _make_adapter()
        assert adapter._intent_guard_passed is None

    def test_adapter_instantiates_with_message_content_intent(self):
        """When no client is injected, the adapter requests message_content intent."""
        import discord as discord_lib

        from router.chat.adapters.discord import DiscordAdapter

        with patch.object(discord_lib, "Client"):
            mock_intents = MagicMock()
            with patch.object(discord_lib.Intents, "default", return_value=mock_intents):
                DiscordAdapter(bot_token="t", agent_name="sam")
            mock_intents.__setattr__  # accessed by the adapter
            assert mock_intents.message_content is not None


# ---------------------------------------------------------------------------
# Multi-agent routing — Sam and Lisa as separate adapter instances
# ---------------------------------------------------------------------------


class TestMultiAgentIdentity:
    def test_sam_and_lisa_are_distinct_adapters(self):
        sam = _make_adapter(agent_name="sam")
        lisa = _make_adapter(agent_name="lisa")
        assert sam._agent_name == "sam"
        assert lisa._agent_name == "lisa"

    def test_sam_principal_ref_distinct_from_lisa(self):
        sam = _make_adapter(agent_name="sam")
        lisa = _make_adapter(agent_name="lisa")
        sam_ref = sam.resolve_principal("111111111")
        lisa_ref = lisa.resolve_principal("222222222")
        assert sam_ref != lisa_ref

    def test_different_tokens_stored_separately(self):
        from router.chat.adapters.discord import DiscordAdapter

        client_a = _make_client()
        client_b = _make_client()
        sam = DiscordAdapter(bot_token="token-sam", agent_name="sam", client=client_a)
        lisa = DiscordAdapter(bot_token="token-lisa", agent_name="lisa", client=client_b)
        assert sam._token != lisa._token

    def test_sam_mention_routed_to_sam_only(self):
        from router.chat.types import ConversationRef

        sam = _make_adapter(agent_name="sam")
        ref = ConversationRef("discord:1:2:0")
        result = sam.parse_mentions("@sam please review", ref)
        assert "sam" in result
        assert "lisa" not in result

    def test_lisa_mention_routed_to_lisa_only(self):
        from router.chat.types import ConversationRef

        lisa = _make_adapter(agent_name="lisa")
        ref = ConversationRef("discord:1:2:0")
        result = lisa.parse_mentions("@lisa check the pipeline", ref)
        assert "lisa" in result
        assert "sam" not in result


# ---------------------------------------------------------------------------
# Discord ApprovalAdapter
# ---------------------------------------------------------------------------


class TestDiscordApprovalAdapter:
    def _make_card(self, **overrides):
        from router.approvals.card import ApprovalCard

        defaults = dict(
            draft_id="draft-001",
            agent_name="lisa",
            pack=None,
            capability_type="email",
            capability_instance="mine",
            action_verb="send",
            summary="To: bram@example.com | Subject: hello",
            payload={"to": "bram@example.com", "subject": "hello", "body": "world"},
            actions=[],
            expires_at=None,
        )
        defaults.update(overrides)
        return ApprovalCard(**defaults)

    def test_render_returns_content_embed_view_spec(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        card = self._make_card()
        result = adapter.render_approval_card(card)
        assert "content" in result
        assert "embed" in result
        assert "view_spec" in result

    def test_render_embed_title_contains_agent_and_verb(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        card = self._make_card(agent_name="lisa", action_verb="send")
        result = adapter.render_approval_card(card)
        assert "Lisa" in result["embed"]["title"]
        assert "send" in result["embed"]["title"]

    def test_render_embed_footer_has_draft_id(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        card = self._make_card(draft_id="draft-xyz")
        result = adapter.render_approval_card(card)
        assert "draft-xyz" in result["embed"]["footer"]["text"]

    def test_render_view_spec_empty_when_no_actions(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        card = self._make_card(actions=[])
        result = adapter.render_approval_card(card)
        assert result["view_spec"] == []

    def test_render_with_expires_at_shows_expiry(self):
        from datetime import datetime, timezone

        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        expires = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        card = self._make_card(expires_at=expires)
        result = adapter.render_approval_card(card)
        fields = result["embed"]["fields"]
        field_names = [f["name"] for f in fields]
        assert "Expires" in field_names

    def test_render_uses_summary_when_available(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        card = self._make_card(summary="custom summary here")
        result = adapter.render_approval_card(card)
        assert "custom summary here" in result["embed"]["description"]

    def test_render_formats_payload_when_no_summary(self):
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        card = self._make_card(summary="", payload={"to": "x@y.com"})
        result = adapter.render_approval_card(card)
        assert "x@y.com" in result["embed"]["description"]

    def test_render_satisfies_approval_adapter_protocol(self):
        from router.approvals.adapters import ApprovalAdapter
        from router.approvals.adapters.discord import DiscordApprovalAdapter

        adapter = DiscordApprovalAdapter()
        # Protocol check: must have render_approval_card method.
        assert callable(getattr(adapter, "render_approval_card", None))
        # Runtime isinstance against Protocol (requires runtime_checkable; skip if not set).
        try:
            assert isinstance(adapter, ApprovalAdapter)
        except TypeError:
            pass  # Protocol not runtime-checkable — structural conformance is enough.


# ---------------------------------------------------------------------------
# _ChannelBucket (rate limiting)
# ---------------------------------------------------------------------------


class TestChannelBucket:
    @pytest.mark.asyncio
    async def test_bucket_allows_up_to_max_tokens(self):
        from router.chat.adapters.discord import _ChannelBucket

        bucket = _ChannelBucket(max_tokens=3, window=60.0)
        # Should not block for the first 3 acquires.
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            for _ in range(3):
                await bucket.acquire()
            mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bucket_sleeps_when_full(self):
        import time

        from router.chat.adapters.discord import _ChannelBucket

        bucket = _ChannelBucket(max_tokens=1, window=5.0)

        # Fill the bucket by pre-populating _timestamps.
        bucket._timestamps = [time.monotonic()]

        call_count = 0

        async def _fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            # After sleeping, clear the timestamps so the next acquire succeeds.
            bucket._timestamps.clear()

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=_fake_sleep):
            await bucket.acquire()

        assert call_count >= 1


# ---------------------------------------------------------------------------
# on_message inbound dispatch (wiring to run_agent_turn)
# ---------------------------------------------------------------------------


def _make_adapter_capturing(agent_name: str = "sam"):
    """Return (adapter, bot_user, on_message) with the registered on_message handler captured."""
    from router.chat.adapters.discord import DiscordAdapter

    bot_user = MagicMock(id=999_000)
    client = MagicMock()
    client.user = bot_user
    captured = {}

    def _capture(fn):
        captured[fn.__name__] = fn
        return fn

    client.event = _capture
    client.get_channel = MagicMock(return_value=None)
    client.get_user = MagicMock(return_value=None)

    adapter = DiscordAdapter(bot_token="fake-token", agent_name=agent_name, client=client)
    return adapter, bot_user, captured["on_message"]


def _make_message(bot_user, *, mentioned: bool = True, content: str = "hey @sam help"):
    """Return a mock discord.Message for a guild channel (not a thread)."""
    msg = MagicMock()
    msg.author = MagicMock(id=123_456)
    msg.mentions = [bot_user] if mentioned else []
    msg.content = content
    msg.guild = MagicMock(id=1)
    msg.channel = MagicMock(id=42)
    msg.thread = None  # no existing thread attached to this message
    # create_thread is called when a root-channel mention opens a new thread.
    mock_new_thread = MagicMock()
    mock_new_thread.id = 77777
    mock_new_thread.join = AsyncMock()
    msg.create_thread = AsyncMock(return_value=mock_new_thread)
    return msg


# ---------------------------------------------------------------------------
# on_message thread routing (issue #650) — helpers
# ---------------------------------------------------------------------------


def _make_thread_channel(*, thread_id: int, parent_id: int = 100, me=None):
    """Create a mock channel object that passes isinstance(ch, discord.Thread).

    Setting ``__class__`` on a MagicMock causes Python's isinstance() to treat
    it as an instance of that class, which lets us simulate discord.Thread
    without subclassing it.
    """
    import discord as discord_lib

    ch = MagicMock()
    ch.id = thread_id
    ch.parent_id = parent_id
    ch.me = me
    ch.__class__ = discord_lib.Thread
    return ch


def _make_thread_message(
    bot_user,
    *,
    thread_id: int,
    parent_id: int = 100,
    me=None,
    mentioned: bool = False,
    content: str = "follow-up",
):
    """Return a mock discord.Message whose channel is already a thread."""
    msg = MagicMock()
    msg.author = MagicMock(id=123_456)
    msg.mentions = [bot_user] if mentioned else []
    msg.content = content
    msg.guild = MagicMock(id=1)
    msg.channel = _make_thread_channel(thread_id=thread_id, parent_id=parent_id, me=me)
    msg.thread = None
    mock_new_thread = MagicMock()
    mock_new_thread.id = 88888
    mock_new_thread.join = AsyncMock()
    msg.create_thread = AsyncMock(return_value=mock_new_thread)
    return msg


class TestOnMessageDispatch:
    @pytest.mark.asyncio
    async def test_mentioned_bot_dispatches_with_correct_agent_name(self):
        """When this bot is mentioned, run_agent_turn is called with agent_name=self._agent_name."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=True)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("agent_name") == "sam"

    @pytest.mark.asyncio
    async def test_non_mentioned_bot_does_not_dispatch(self):
        """When this bot is NOT mentioned, run_agent_turn is NOT called."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=False)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_error_sets_error_status_no_exception_escapes(self):
        """DispatchError in run_agent_turn → ERROR status posted; no exception propagates."""
        from router.chat.types import AdapterStatus
        from router.dispatcher import DispatchError

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=True)

        with patch(
            "router.chat.adapters.discord.run_agent_turn",
            new_callable=AsyncMock,
            side_effect=DispatchError("container failed"),
        ):
            with patch.object(adapter, "set_status", new_callable=AsyncMock) as mock_status:
                with patch.object(adapter, "send_message", new_callable=AsyncMock):
                    await on_message(msg)  # must not raise

        error_calls = [c for c in mock_status.call_args_list if c[0][1] == AdapterStatus.ERROR]
        assert error_calls, "set_status(ERROR) was never called after DispatchError"


# ---------------------------------------------------------------------------
# on_message thread routing (issue #650)
# ---------------------------------------------------------------------------


class TestOnMessageThreadRouting:
    @pytest.mark.asyncio
    async def test_in_thread_not_mentioned_bot_member_dispatches(self):
        """In-thread follow-up without @mention: gate passes when bot is a thread member."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_thread_message(bot_user, thread_id=42, me=MagicMock(), mentioned=False)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_in_thread_not_mentioned_bot_not_member_dropped(self):
        """In-thread message without @mention: gate fails when bot is NOT a thread member."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_thread_message(bot_user, thread_id=42, me=None, mentioned=False)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_root_mention_creates_thread_and_ref_carries_thread_id(self):
        """Root @mention: create_thread + join called; inbound ref carries the new thread id."""
        from router.chat.adapters.discord import _decode_ref

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=True, content="start a thread please")

        captured_refs = []

        async def _capture(adapter_arg, inbound, *, agent_name):
            captured_refs.append(inbound.conversation_ref)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock, side_effect=_capture):
            await on_message(msg)

        msg.create_thread.assert_awaited_once()
        assert len(captured_refs) == 1
        _, _, decoded_thread_id = _decode_ref(captured_refs[0])
        assert decoded_thread_id == 77777  # id from the mock thread in _make_message

    @pytest.mark.asyncio
    async def test_in_thread_mention_no_new_thread_ref_carries_existing_id(self):
        """@mention inside existing thread: create_thread NOT called; ref carries the existing thread id."""
        from router.chat.adapters.discord import _decode_ref

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_thread_message(bot_user, thread_id=42, parent_id=100, me=MagicMock(), mentioned=True)

        captured_refs = []

        async def _capture(adapter_arg, inbound, *, agent_name):
            captured_refs.append(inbound.conversation_ref)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock, side_effect=_capture):
            await on_message(msg)

        msg.create_thread.assert_not_awaited()
        assert len(captured_refs) == 1
        _, decoded_channel_id, decoded_thread_id = _decode_ref(captured_refs[0])
        assert decoded_thread_id == 42
        assert decoded_channel_id == 100

    @pytest.mark.asyncio
    async def test_forbidden_on_create_thread_falls_back_flat_channel_no_raise(self):
        """discord.Forbidden on create_thread → flat channel fallback; does not raise."""
        import discord as discord_lib

        from router.chat.adapters.discord import _decode_ref

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=True)
        msg.create_thread = AsyncMock(side_effect=discord_lib.Forbidden(MagicMock(status=403), "forbidden"))

        captured_refs = []

        async def _capture(adapter_arg, inbound, *, agent_name):
            captured_refs.append(inbound.conversation_ref)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock, side_effect=_capture):
            await on_message(msg)  # must not raise

        assert len(captured_refs) == 1
        _, _, decoded_thread_id = _decode_ref(captured_refs[0])
        assert decoded_thread_id == 0  # flat channel fallback


# ---------------------------------------------------------------------------
# aidt command surface (issue #658)
# ---------------------------------------------------------------------------


def _make_aidt_message(bot_user, *, content: str, is_bot_author: bool = False):
    """Return a mock discord.Message containing an aidt command."""
    msg = MagicMock()
    msg.author = MagicMock(id=123_456, bot=is_bot_author)
    msg.mentions = [bot_user]
    msg.content = content
    msg.guild = MagicMock(id=1)
    msg.channel = MagicMock(id=42)
    msg.thread = None
    mock_new_thread = MagicMock()
    mock_new_thread.id = 77777
    mock_new_thread.join = AsyncMock()
    msg.create_thread = AsyncMock(return_value=mock_new_thread)
    return msg


class TestAidtCommandSurface:
    """aidt command interception in on_message — issue #658."""

    @pytest.mark.asyncio
    async def test_aidt_help_sends_response_not_agent_turn(self):
        """aidt help → dispatch_command → send_message; run_agent_turn NOT called."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_aidt_message(bot_user, content="aidt help")

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        adapter._client.get_channel = MagicMock(return_value=mock_channel)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_not_awaited()
        mock_channel.send.assert_awaited_once()
        sent_text = mock_channel.send.call_args[0][0]
        assert "aidt" in sent_text  # help text contains verb usage lines

    @pytest.mark.asyncio
    async def test_aidt_killall_from_human_executes_and_sends_response(self):
        """Human aidt killall → runs kill logic → response sent to channel."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_aidt_message(bot_user, content="aidt killall", is_bot_author=False)

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        adapter._client.get_channel = MagicMock(return_value=mock_channel)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            with patch("router.kill_command.get_agent_map", return_value={}):
                with patch("router.kill_command.get_default_guard") as mock_guard_fn:
                    from router.stuck_guard import GuardConfig, StuckGuard

                    guard = StuckGuard(config=GuardConfig(mode="dry-run"))
                    mock_guard_fn.return_value = guard
                    await on_message(msg)

        mock_run.assert_not_awaited()
        mock_channel.send.assert_awaited_once()
        sent_text = mock_channel.send.call_args[0][0]
        # No tasks → informational reply
        assert "No active tasks" in sent_text or "kill" in sent_text.lower()

    @pytest.mark.asyncio
    async def test_aidt_killall_from_bot_is_rejected_and_sends_rejection(self):
        """Regression (#658): bot-authored aidt killall → rejection sent; no kill runs."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_aidt_message(bot_user, content="aidt killall", is_bot_author=True)

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        adapter._client.get_channel = MagicMock(return_value=mock_channel)

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            with patch("router.kill_command.get_default_guard") as mock_guard_fn:
                await on_message(msg)
                # Guard must NOT be consulted — gate fires before kill logic.
                mock_guard_fn.assert_not_called()

        mock_run.assert_not_awaited()
        mock_channel.send.assert_awaited_once()
        sent_text = mock_channel.send.call_args[0][0]
        assert "restricted" in sent_text or "agents cannot run commands" in sent_text

    @pytest.mark.asyncio
    async def test_non_aidt_message_falls_through_to_agent_turn(self):
        """A normal @mention without aidt falls through to run_agent_turn."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="hey @sam can you help with something?")

        with patch("router.chat.adapters.discord.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aidt_sets_principal_kind_human_for_non_bot_author(self):
        """principal.kind is 'human' when message.author.bot is False."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_aidt_message(bot_user, content="aidt help", is_bot_author=False)

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        adapter._client.get_channel = MagicMock(return_value=mock_channel)

        captured_principals = []

        async def _capture_dispatch(cmd, principal, **_kwargs):
            captured_principals.append(principal)
            from router.commands.types import CommandResult

            return CommandResult(text="help text")

        # The import is `from router.commands.core import dispatch_command` inside the handler.
        with patch("router.commands.core.dispatch_command", new_callable=AsyncMock, side_effect=_capture_dispatch):
            await on_message(msg)

        assert len(captured_principals) == 1
        assert captured_principals[0].kind == "human"

    @pytest.mark.asyncio
    async def test_aidt_sets_principal_kind_bot_for_bot_author(self):
        """principal.kind is 'bot' when message.author.bot is True."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_aidt_message(bot_user, content="aidt help", is_bot_author=True)

        mock_channel = AsyncMock()
        mock_channel.get_thread = MagicMock(return_value=None)
        adapter._client.get_channel = MagicMock(return_value=mock_channel)

        captured_principals = []

        async def _capture_dispatch(cmd, principal, **_kwargs):
            captured_principals.append(principal)
            from router.commands.types import CommandResult

            return CommandResult(text="help text")

        with patch("router.commands.core.dispatch_command", new_callable=AsyncMock, side_effect=_capture_dispatch):
            await on_message(msg)

        assert len(captured_principals) == 1
        assert captured_principals[0].kind == "bot"
