"""Tests for the ChatAdapter contract — interface, types, Slack stub, and feature flag.

These are purely additive tests against new code. No existing Slack path is
exercised or modified.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Import smoke — everything must be importable
# ---------------------------------------------------------------------------


class TestImports:
    def test_chat_package_importable(self):
        import router.chat  # noqa: F401

    def test_types_importable(self):
        from router.chat.types import (  # noqa: F401
            AdapterCapabilities,
            AdapterStatus,
            ConversationRef,
            InboundMessage,
            OutboundMessage,
            PrincipalRef,
            PromptChoice,
            StructuredResponse,
        )

    def test_interface_importable(self):
        from router.chat.interface import ChatAdapter  # noqa: F401

    def test_slack_adapter_importable(self):
        from router.chat.adapters.slack import SlackAdapter  # noqa: F401


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


class TestConversationRef:
    def test_is_str_subtype(self):
        from router.chat.types import ConversationRef

        ref = ConversationRef("C123:12345.6789")
        assert isinstance(ref, str)

    def test_persistable_as_string(self):
        from router.chat.types import ConversationRef

        ref = ConversationRef("C_GENERAL:1000000.0001")
        assert str(ref) == "C_GENERAL:1000000.0001"


class TestPrincipalRef:
    def test_is_str_subtype(self):
        from router.chat.types import PrincipalRef

        ref = PrincipalRef("U01234ABC")
        assert isinstance(ref, str)

    def test_persistable_as_string(self):
        from router.chat.types import PrincipalRef

        ref = PrincipalRef("U01234ABC")
        assert str(ref) == "U01234ABC"


class TestAdapterStatus:
    def test_all_variants_present(self):
        from router.chat.types import AdapterStatus

        assert AdapterStatus.THINKING
        assert AdapterStatus.WORKING
        assert AdapterStatus.DONE
        assert AdapterStatus.ERROR

    def test_is_str_enum(self):
        from router.chat.types import AdapterStatus

        assert AdapterStatus.THINKING == "thinking"
        assert AdapterStatus.ERROR == "error"


class TestAdapterCapabilities:
    def test_defaults_are_all_false(self):
        from router.chat.types import AdapterCapabilities

        caps = AdapterCapabilities()
        assert caps.supports_threads is False
        assert caps.supports_channels is False
        assert caps.supports_interactive is False
        assert caps.supports_forms is False

    def test_immutable(self):
        from router.chat.types import AdapterCapabilities

        caps = AdapterCapabilities(supports_threads=True)
        with pytest.raises((AttributeError, TypeError)):
            caps.supports_threads = False  # type: ignore[misc]


class TestInboundMessage:
    def test_construction(self):
        from router.chat.types import ConversationRef, InboundMessage, PrincipalRef

        msg = InboundMessage(
            conversation_ref=ConversationRef("C123:1000"),
            principal_ref=PrincipalRef("U999"),
            text="hello",
        )
        assert msg.text == "hello"
        assert msg.attachments == []

    def test_attachments_default_empty(self):
        from router.chat.types import ConversationRef, InboundMessage, PrincipalRef

        msg = InboundMessage(
            conversation_ref=ConversationRef("C1:1"),
            principal_ref=PrincipalRef("U1"),
            text="hi",
        )
        assert msg.attachments == []


class TestOutboundMessage:
    def test_with_ref(self):
        from router.chat.types import ConversationRef, OutboundMessage

        msg = OutboundMessage(text="reply", conversation_ref=ConversationRef("C1:1"))
        assert msg.text == "reply"
        assert msg.conversation_ref is not None

    def test_proactive_no_ref(self):
        from router.chat.types import OutboundMessage

        msg = OutboundMessage(text="PR merged!")
        assert msg.conversation_ref is None


class TestPromptChoice:
    def test_defaults(self):
        from router.chat.types import PromptChoice

        p = PromptChoice(prompt="Pick one", choices=["a", "b"])
        assert p.timeout_seconds == 60

    def test_custom_timeout(self):
        from router.chat.types import PromptChoice

        p = PromptChoice(prompt="Pick one", choices=["a", "b"], timeout_seconds=30)
        assert p.timeout_seconds == 30


class TestStructuredResponse:
    def test_fields(self):
        from router.chat.types import StructuredResponse

        resp = StructuredResponse(choice="b", index=1)
        assert resp.choice == "b"
        assert resp.index == 1


# ---------------------------------------------------------------------------
# Interface — ChatAdapter is abstract
# ---------------------------------------------------------------------------


class TestChatAdapterAbstract:
    def test_cannot_instantiate_directly(self):
        from router.chat.interface import ChatAdapter

        with pytest.raises(TypeError):
            ChatAdapter()  # type: ignore[abstract]

    def test_abstract_methods_declared(self):
        from router.chat.interface import ChatAdapter

        abstract_names = getattr(ChatAdapter, "__abstractmethods__", set())
        required = {
            "capabilities",
            "send_message",
            "read_thread",
            "set_status",
            "resolve_principal",
            "parse_mentions",
            "prompt_for_choice",
            "collect_input",
        }
        assert required.issubset(abstract_names), f"Missing abstract methods: {required - abstract_names}"


# ---------------------------------------------------------------------------
# Slack adapter — contract shape (behavioral tests live in test_slack_adapter.py)
# ---------------------------------------------------------------------------


def _slack_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.2"})
    client.assistant_threads_setStatus = AsyncMock(return_value={"ok": True})
    return client


class TestSlackAdapterIsSubclass:
    def test_is_chat_adapter(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.interface import ChatAdapter

        assert issubclass(SlackAdapter, ChatAdapter)

    def test_instantiates_with_agent_and_client(self):
        from router.chat.adapters.slack import SlackAdapter

        adapter = SlackAdapter("sam", _slack_client())
        assert adapter is not None

    def test_instantiates_with_default_channel(self):
        from router.chat.adapters.slack import SlackAdapter

        adapter = SlackAdapter("sam", _slack_client(), default_channel="C_GENERAL")
        assert adapter._default_channel == "C_GENERAL"


class TestSlackAdapterCapabilities:
    def test_supports_threads(self):
        from router.chat.adapters.slack import SlackAdapter

        assert SlackAdapter("sam", _slack_client()).capabilities.supports_threads is True

    def test_supports_channels(self):
        from router.chat.adapters.slack import SlackAdapter

        assert SlackAdapter("sam", _slack_client()).capabilities.supports_channels is True

    def test_supports_interactive(self):
        from router.chat.adapters.slack import SlackAdapter

        assert SlackAdapter("sam", _slack_client()).capabilities.supports_interactive is True

    def test_supports_forms(self):
        from router.chat.adapters.slack import SlackAdapter

        # #747: collect_input is implemented natively (views.open modal when a
        # trigger_id is present, pending-input scripted Q&A otherwise).
        assert SlackAdapter("sam", _slack_client()).capabilities.supports_forms is True


class TestSlackAdapterResolvePrincipal:
    def test_wraps_user_id(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import PrincipalRef

        adapter = SlackAdapter("sam", _slack_client())
        ref = adapter.resolve_principal("U01234ABC")
        assert ref == PrincipalRef("U01234ABC")
        assert isinstance(ref, str)


class TestSlackAdapterParseMentions:
    def test_returns_empty_list_when_no_agents_known(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import ConversationRef

        adapter = SlackAdapter("sam", _slack_client())
        with (
            patch("router.chat.adapters.slack.get_agent_map", return_value={}),
            patch("router.runtime.bot_user_map", {}),
        ):
            result = adapter.parse_mentions("hey @lisa", ConversationRef("C1:1"))
        assert result == []


class TestSlackAdapterAsync:
    @pytest.mark.asyncio
    async def test_send_message_no_ref_uses_default_channel(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import OutboundMessage

        client = _slack_client()
        adapter = SlackAdapter("sam", client, default_channel="C_HOME")
        await adapter.send_message(OutboundMessage(text="hello"))
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C_HOME"
        assert kwargs["thread_ts"] is None

    @pytest.mark.asyncio
    async def test_send_message_with_ref(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import ConversationRef, OutboundMessage

        client = _slack_client()
        adapter = SlackAdapter("sam", client)
        await adapter.send_message(OutboundMessage(text="reply", conversation_ref=ConversationRef("C1:1000")))
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "1000"

    @pytest.mark.asyncio
    async def test_read_thread_returns_list(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import ConversationRef

        adapter = SlackAdapter("sam", _slack_client())
        with (
            patch("router.chat.adapters.slack.load_thread_history", new=AsyncMock(return_value=[])),
            patch("router.chat.adapters.slack.get_agent_map", return_value={}),
            patch("router.runtime.bot_user_map", {}),
        ):
            result = await adapter.read_thread(ConversationRef("C1:1000"))
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_set_status_all_states(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import AdapterStatus, ConversationRef

        adapter = SlackAdapter("sam", _slack_client())
        ref = ConversationRef("C1:1000")
        with patch("router.chat.adapters.slack.get_agent_map", return_value={}):
            for state in AdapterStatus:
                await adapter.set_status(ref, state)

    @pytest.mark.asyncio
    async def test_prompt_for_choice_returns_first_choice(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import ConversationRef, PromptChoice

        adapter = SlackAdapter("sam", _slack_client())
        prompt = PromptChoice(prompt="Pick one", choices=["alpha", "beta", "gamma"])
        resp = await adapter.prompt_for_choice(ConversationRef("C1:1000"), prompt)
        assert resp.choice == "alpha"
        assert resp.index == 0

    @pytest.mark.asyncio
    async def test_collect_input_without_trigger_uses_scripted_fallback(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import ConversationRef, InputField, InputRequest

        with (
            patch("router.chat.adapters.slack.get_agent_map", return_value={}),
            patch("router.runtime.bot_user_map", {}),
        ):
            adapter = SlackAdapter("sam", _slack_client())
            request = InputRequest(
                title="Setup",
                fields=[InputField(key="name", label="Name?")],
                timeout_seconds=0.05,
            )
            resp = await adapter.collect_input(ConversationRef("C1:1000"), request)
        # No reply is ever delivered via pending_input, so the scripted
        # fallback times out — proves collect_input actually drove
        # input_collect rather than being a stub returning a fixed value.
        assert resp.status == "timed_out"

    @pytest.mark.asyncio
    async def test_collect_input_without_trigger_consumes_pending_reply(self):
        """The no-trigger path is push-based: slack_events resolves the user's
        next thread message into ``pending_input`` (and consumes it), keyed by
        the adapter's own ref encoding."""
        import asyncio

        from router.chat import pending_input
        from router.chat.adapters.slack import SlackAdapter, make_inbound_ref
        from router.chat.types import InputField, InputRequest

        with (
            patch("router.chat.adapters.slack.get_agent_map", return_value={}),
            patch("router.runtime.bot_user_map", {}),
            patch("router.chat.adapters.slack.outbound_mention_ids", new=AsyncMock(return_value={})),
        ):
            adapter = SlackAdapter("sam", _slack_client())
            ref = make_inbound_ref("C1", "1000")
            request = InputRequest(title="", fields=[InputField(key="pat", label="Paste the token")])

            async def deliver_reply():
                for _ in range(200):
                    # The same call slack_events makes when the user's next
                    # thread message arrives.
                    if pending_input.resolve_reply(str(make_inbound_ref("C1", "1000")), "ghp_reply"):
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("collector never registered a pending reply")

            resp, _ = await asyncio.gather(adapter.collect_input(ref, request), deliver_reply())

        assert resp.status == "completed"
        assert resp.values == {"pat": "ghp_reply"}

    @pytest.mark.asyncio
    async def test_collect_input_with_trigger_opens_modal(self):
        from router.chat.adapters.slack import SlackAdapter
        from router.chat.types import ConversationRef, InputField, InputRequest, InputResponse

        adapter = SlackAdapter("sam", _slack_client(), trigger_id="T123")
        request = InputRequest(title="Setup", fields=[InputField(key="name", label="Name?")])
        canned = InputResponse(values={"name": "Ada"}, status="completed")
        with patch("router.chat.adapters.slack.open_input_request", new=AsyncMock(return_value=canned)) as open_request:
            resp = await adapter.collect_input(ConversationRef("C1:1000"), request)

        assert resp is canned
        open_request.assert_awaited_once_with(adapter._client, "T123", request)


class TestMakeInboundRef:
    def test_encodes_channel_and_thread(self):
        from router.chat.adapters.slack import make_inbound_ref

        ref = make_inbound_ref("C_GENERAL", "1234567890.000100")
        # ConversationRef is a NewType(str), so the underlying value is a str.
        assert isinstance(ref, str)
        assert "C_GENERAL" in ref
        assert "1234567890.000100" in ref


# ---------------------------------------------------------------------------
# Feature flag scaffold
# ---------------------------------------------------------------------------


class TestChatBackendsFlag:
    def test_default_is_false(self, monkeypatch):
        monkeypatch.delenv("CHAT_BACKENDS", raising=False)
        import router.chat as chat_pkg

        importlib.reload(chat_pkg)
        assert chat_pkg.CHAT_BACKENDS is False

    def test_truthy_string_enables_flag(self, monkeypatch):
        monkeypatch.setenv("CHAT_BACKENDS", "true")
        import router.chat as chat_pkg

        importlib.reload(chat_pkg)
        assert chat_pkg.CHAT_BACKENDS is True

    def test_one_enables_flag(self, monkeypatch):
        monkeypatch.setenv("CHAT_BACKENDS", "1")
        import router.chat as chat_pkg

        importlib.reload(chat_pkg)
        assert chat_pkg.CHAT_BACKENDS is True

    def test_falsy_string_stays_false(self, monkeypatch):
        monkeypatch.setenv("CHAT_BACKENDS", "false")
        import router.chat as chat_pkg

        importlib.reload(chat_pkg)
        assert chat_pkg.CHAT_BACKENDS is False
