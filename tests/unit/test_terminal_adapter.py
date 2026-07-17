"""Tests for the terminal ChatAdapter and E2E driver.

All four primitives exercised inbound → principal → outbound → interactive
through the real TerminalAdapter — no Slack creds, no network.
"""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Import smoke
# ---------------------------------------------------------------------------


class TestImports:
    def test_terminal_adapter_importable(self):
        from router.chat.adapters.terminal import TerminalAdapter  # noqa: F401

    def test_make_inbound_ref_importable(self):
        from router.chat.adapters.terminal import make_inbound_ref  # noqa: F401

    def test_terminal_driver_importable(self):
        from router.chat.terminal_driver import run_once  # noqa: F401


# ---------------------------------------------------------------------------
# ConversationRef encoding
# ---------------------------------------------------------------------------


class TestTerminalRef:
    def test_make_inbound_ref_contains_session_id(self):
        from router.chat.adapters.terminal import make_inbound_ref

        ref = make_inbound_ref("sess-abc")
        assert "sess-abc" in ref

    def test_make_inbound_ref_is_not_slack_format(self):
        """Terminal ref must not be channel:thread (Slack encoding)."""
        from router.chat.adapters.terminal import make_inbound_ref

        ref = make_inbound_ref("sess-abc")
        # Slack format is "<channel_id>:<thread_ts>" — no "terminal:" prefix.
        assert ref.startswith("terminal:")

    def test_make_inbound_ref_is_str(self):
        from router.chat.adapters.terminal import make_inbound_ref

        ref = make_inbound_ref("x")
        assert isinstance(ref, str)

    def test_two_sessions_produce_different_refs(self):
        from router.chat.adapters.terminal import make_inbound_ref

        assert make_inbound_ref("session-1") != make_inbound_ref("session-2")


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestTerminalCapabilities:
    def test_is_chat_adapter_subclass(self):
        from router.chat.adapters.terminal import TerminalAdapter
        from router.chat.interface import ChatAdapter

        assert issubclass(TerminalAdapter, ChatAdapter)

    def test_no_threads(self):
        from router.chat.adapters.terminal import TerminalAdapter

        assert TerminalAdapter().capabilities.supports_threads is False

    def test_no_channels(self):
        from router.chat.adapters.terminal import TerminalAdapter

        assert TerminalAdapter().capabilities.supports_channels is False

    def test_supports_interactive_false(self):
        """supports_interactive=False tells core not to use the button path."""
        from router.chat.adapters.terminal import TerminalAdapter

        assert TerminalAdapter().capabilities.supports_interactive is False


# ---------------------------------------------------------------------------
# PrincipalRef — not a Slack user ID
# ---------------------------------------------------------------------------


class TestTerminalPrincipal:
    def test_resolve_principal_has_local_prefix(self):
        """PrincipalRef must not look like a Slack user ID (U…)."""
        from router.chat.adapters.terminal import TerminalAdapter

        adapter = TerminalAdapter(username="alice")
        ref = adapter.resolve_principal("alice")
        assert ref.startswith("local:")

    def test_resolve_principal_is_str(self):
        from router.chat.adapters.terminal import TerminalAdapter
        from router.chat.types import PrincipalRef

        adapter = TerminalAdapter()
        ref = adapter.resolve_principal("bob")
        assert isinstance(ref, str)
        assert ref == PrincipalRef("local:bob")

    def test_different_usernames_produce_different_refs(self):
        from router.chat.adapters.terminal import TerminalAdapter

        adapter = TerminalAdapter()
        assert adapter.resolve_principal("alice") != adapter.resolve_principal("bob")


# ---------------------------------------------------------------------------
# parse_mentions
# ---------------------------------------------------------------------------


class TestTerminalParseMentions:
    def test_extracts_at_mention(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref

        adapter = TerminalAdapter()
        ref = make_inbound_ref("s1")
        assert adapter.parse_mentions("hey @lisa can you help?", ref) == ["lisa"]

    def test_extracts_multiple_mentions_in_order(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref

        adapter = TerminalAdapter()
        ref = make_inbound_ref("s1")
        assert adapter.parse_mentions("@lisa and @sam please review", ref) == ["lisa", "sam"]

    def test_no_mentions_returns_empty(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref

        adapter = TerminalAdapter()
        ref = make_inbound_ref("s1")
        assert adapter.parse_mentions("no mentions here", ref) == []


# ---------------------------------------------------------------------------
# Outbound — send_message
# ---------------------------------------------------------------------------


class TestTerminalSendMessage:
    @pytest.mark.asyncio
    async def test_send_with_ref_prints_session_prefix(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import OutboundMessage

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("my-session")
        await adapter.send_message(OutboundMessage(text="hello", conversation_ref=ref))
        assert "my-session" in out.getvalue()
        assert "hello" in out.getvalue()

    @pytest.mark.asyncio
    async def test_send_without_ref_prints_bare(self):
        """Proactive / cron shape: no conversation_ref."""
        from router.chat.adapters.terminal import TerminalAdapter
        from router.chat.types import OutboundMessage

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        await adapter.send_message(OutboundMessage(text="background task done"))
        value = out.getvalue()
        assert "background task done" in value
        # No session prefix for proactive messages
        assert "terminal:" not in value


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestTerminalSetStatus:
    @pytest.mark.asyncio
    async def test_all_states_produce_output(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import AdapterStatus

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("s1")
        for state in AdapterStatus:
            await adapter.set_status(ref, state)
        assert len(out.getvalue()) > 0

    @pytest.mark.asyncio
    async def test_thinking_label(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import AdapterStatus

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        await adapter.set_status(make_inbound_ref("s"), AdapterStatus.THINKING)
        assert "..." in out.getvalue()


# ---------------------------------------------------------------------------
# read_thread / record_inbound
# ---------------------------------------------------------------------------


class TestTerminalReadThread:
    @pytest.mark.asyncio
    async def test_empty_history_initially(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref

        adapter = TerminalAdapter()
        ref = make_inbound_ref("s1")
        history = await adapter.read_thread(ref)
        assert history == []

    @pytest.mark.asyncio
    async def test_records_and_retrieves_inbound(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InboundMessage

        adapter = TerminalAdapter()
        ref = make_inbound_ref("s1")
        principal = adapter.resolve_principal("alice")
        msg = InboundMessage(conversation_ref=ref, principal_ref=principal, text="hi")
        adapter.record_inbound(msg)
        history = await adapter.read_thread(ref)
        assert len(history) == 1
        assert history[0].text == "hi"

    @pytest.mark.asyncio
    async def test_read_thread_returns_copy(self):
        """Mutation of the returned list must not affect internal history."""
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InboundMessage

        adapter = TerminalAdapter()
        ref = make_inbound_ref("s1")
        principal = adapter.resolve_principal("alice")
        adapter.record_inbound(InboundMessage(conversation_ref=ref, principal_ref=principal, text="a"))
        history = await adapter.read_thread(ref)
        history.clear()
        assert len(await adapter.read_thread(ref)) == 1


# ---------------------------------------------------------------------------
# Interactive primitive — prompt_for_choice
# ---------------------------------------------------------------------------


class TestTerminalPromptForChoice:
    @pytest.mark.asyncio
    async def test_numbered_list_printed(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import PromptChoice

        out = io.StringIO()
        fake_in = io.StringIO("2\n")
        adapter = TerminalAdapter(output=out, input=fake_in)
        ref = make_inbound_ref("s1")
        await adapter.prompt_for_choice(ref, PromptChoice(prompt="Pick one", choices=["alpha", "beta", "gamma"]))
        text = out.getvalue()
        assert "1." in text
        assert "2." in text
        assert "3." in text
        assert "alpha" in text
        assert "beta" in text

    @pytest.mark.asyncio
    async def test_valid_selection_returned(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import PromptChoice

        out = io.StringIO()
        fake_in = io.StringIO("2\n")
        adapter = TerminalAdapter(output=out, input=fake_in)
        ref = make_inbound_ref("s1")
        resp = await adapter.prompt_for_choice(ref, PromptChoice(prompt="Pick", choices=["a", "b", "c"]))
        assert resp.choice == "b"
        assert resp.index == 1

    @pytest.mark.asyncio
    async def test_invalid_input_defaults_to_first(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import PromptChoice

        out = io.StringIO()
        fake_in = io.StringIO("not-a-number\n")
        adapter = TerminalAdapter(output=out, input=fake_in)
        ref = make_inbound_ref("s1")
        resp = await adapter.prompt_for_choice(ref, PromptChoice(prompt="Pick", choices=["x", "y"]))
        assert resp.index == 0
        assert resp.choice == "x"

    @pytest.mark.asyncio
    async def test_out_of_range_defaults_to_first(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import PromptChoice

        out = io.StringIO()
        fake_in = io.StringIO("99\n")
        adapter = TerminalAdapter(output=out, input=fake_in)
        ref = make_inbound_ref("s1")
        resp = await adapter.prompt_for_choice(ref, PromptChoice(prompt="Pick", choices=["x", "y"]))
        assert resp.index == 0

    @pytest.mark.asyncio
    async def test_returns_structured_response(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import PromptChoice, StructuredResponse

        out = io.StringIO()
        fake_in = io.StringIO("1\n")
        adapter = TerminalAdapter(output=out, input=fake_in)
        ref = make_inbound_ref("s1")
        resp = await adapter.prompt_for_choice(ref, PromptChoice(prompt="Pick", choices=["only"]))
        assert isinstance(resp, StructuredResponse)


# ---------------------------------------------------------------------------
# Structured-input primitive — collect_input (supports_forms=False → scripted)
# ---------------------------------------------------------------------------


class TestTerminalCollectInput:
    @pytest.mark.asyncio
    async def test_supports_forms_is_false(self):
        from router.chat.adapters.terminal import TerminalAdapter

        assert TerminalAdapter().capabilities.supports_forms is False

    @pytest.mark.asyncio
    async def test_delegates_to_scripted_fallback(self):
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InputField, InputRequest

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("s1")

        request = InputRequest(
            title="Setup",
            fields=[InputField(key="name", label="Name?")],
            timeout_seconds=0.05,
        )
        resp = await adapter.collect_input(ref, request)
        # No reply ever arrives, so the scripted fallback times out — proves
        # collect_input actually drove input_collect rather than being a stub
        # that returns a fixed value.
        assert resp.status == "timed_out"


# ---------------------------------------------------------------------------
# E2E driver — all four primitives in one pass
# ---------------------------------------------------------------------------


class TestTerminalDriverE2E:
    """Drive inbound → principal → outbound → interactive through the real adapter."""

    @pytest.mark.asyncio
    async def test_run_once_exercises_all_primitives(self):
        """Single run_once call must produce output for every primitive."""
        from router.chat.terminal_driver import run_once

        out = io.StringIO()
        # Inject "2" as the interactive reply (select second choice: "Stop")
        fake_in = io.StringIO("2\n")
        from router.chat.adapters.terminal import TerminalAdapter

        adapter = TerminalAdapter(output=out, input=fake_in)
        await run_once(adapter, "hello @lisa", session_id="test-sess")

        text = out.getvalue()
        # 2. Status: THINKING
        assert "..." in text
        # 3. Outbound reply with session prefix
        assert "test-sess" in text
        assert "Echo: hello @lisa" in text
        # 4. Proactive emission (no ref — bare text)
        assert "proactive" in text
        # 5. Interactive: numbered list rendered + choice echoed
        assert "1." in text
        assert "Stop" in text
        assert "You chose: Stop" in text
        # 6. Status: DONE
        assert "done" in text

    @pytest.mark.asyncio
    async def test_conversation_ref_is_not_slack_format(self):
        """run_once must build a ref that is not channel:thread."""
        from router.chat.adapters.terminal import TerminalAdapter

        out = io.StringIO()
        fake_in = io.StringIO("1\n")
        adapter = TerminalAdapter(output=out, input=fake_in)

        from router.chat.terminal_driver import run_once

        await run_once(adapter, "test", session_id="s42")
        # The session id must appear in output; the Slack separator ":" alone
        # proves nothing — we check the terminal: prefix appears in history.
        assert len(adapter._history) == 1
        ref = adapter._history[0].conversation_ref
        assert ref.startswith("terminal:")
        assert "s42" in ref

    @pytest.mark.asyncio
    async def test_principal_ref_is_not_slack_user_id(self):
        """run_once must build a PrincipalRef that starts with 'local:'."""
        from router.chat.adapters.terminal import TerminalAdapter

        out = io.StringIO()
        fake_in = io.StringIO("1\n")
        adapter = TerminalAdapter(username="tester", output=out, input=fake_in)

        from router.chat.terminal_driver import run_once

        await run_once(adapter, "msg", session_id="s1")
        assert len(adapter._history) == 1
        principal = adapter._history[0].principal_ref
        assert principal.startswith("local:")

    @pytest.mark.asyncio
    async def test_proactive_emission_has_no_ref(self):
        """The proactive send_message call must use conversation_ref=None."""
        from router.chat.adapters.terminal import TerminalAdapter
        from router.chat.types import OutboundMessage

        sent: list[OutboundMessage] = []
        out = io.StringIO()
        fake_in = io.StringIO("1\n")
        adapter = TerminalAdapter(output=out, input=fake_in)

        # Patch send_message to capture calls
        original_send = adapter.send_message

        async def capturing_send(outbound: OutboundMessage) -> None:
            sent.append(outbound)
            await original_send(outbound)

        adapter.send_message = capturing_send  # type: ignore[method-assign]

        from router.chat.terminal_driver import run_once

        await run_once(adapter, "hi", session_id="s1")
        proactive = [m for m in sent if m.conversation_ref is None]
        assert len(proactive) >= 1, "Expected at least one proactive send with no ref"

    @pytest.mark.asyncio
    async def test_interactive_choice_parsed_and_returned(self):
        """prompt_for_choice must parse a numeric reply and echo the selection."""
        from router.chat.adapters.terminal import TerminalAdapter

        out = io.StringIO()
        # Select choice 3 ("Restart")
        fake_in = io.StringIO("3\n")
        adapter = TerminalAdapter(output=out, input=fake_in)

        from router.chat.terminal_driver import run_once

        await run_once(adapter, "go", session_id="s1")
        assert "You chose: Restart" in out.getvalue()
