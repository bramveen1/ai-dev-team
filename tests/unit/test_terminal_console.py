"""Tests for the transport-agnostic core seam and terminal console.

Covers:
  - run_agent_turn: agent resolution, adapter integration, CLI dispatch,
    response delivery, error handling.
  - terminal_console: importability and entry-point wiring.
"""

from __future__ import annotations

import io
import json
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Import smoke
# ---------------------------------------------------------------------------


class TestImports:
    def test_core_importable(self):
        from router.chat.core import run_agent_turn  # noqa: F401

    def test_terminal_console_importable(self):
        import router.chat.terminal_console  # noqa: F401

    def test_terminal_console_has_main(self):
        from router.chat.terminal_console import main  # noqa: F401


# ---------------------------------------------------------------------------
# run_agent_turn — agent name resolution
# ---------------------------------------------------------------------------


FAKE_AGENT_MAP = {
    "sam": {
        "container": "sam-container",
        "name": "Sam",
        "model": None,
        "container_timeout": None,
    },
    "lisa": {
        "container": "lisa-container",
        "name": "Lisa",
        "model": None,
        "container_timeout": None,
    },
}

FAKE_MEMORY = {"org_memory": "", "agent_memory": ""}

_GOOD_JSON = json.dumps({"result": "Hello from Sam!"})


def _make_adapter(*, output: io.StringIO | None = None, agent_name_in_text: str | None = None):
    """Return a TerminalAdapter with controlled I/O."""
    from router.chat.adapters.terminal import TerminalAdapter

    out = output or io.StringIO()
    return TerminalAdapter(output=out)


def _make_inbound(text: str = "hi"):
    from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
    from router.chat.types import InboundMessage

    adapter = TerminalAdapter()
    ref = make_inbound_ref("test-session")
    principal = adapter.resolve_principal("tester")
    return InboundMessage(conversation_ref=ref, principal_ref=principal, text=text)


class TestRunAgentTurnAgentResolution:
    @pytest.mark.asyncio
    async def test_explicit_agent_name_used(self):
        """Explicit agent_name kwarg bypasses mention parsing."""
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("no mentions here")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            result = await __import__("router.chat.core", fromlist=["run_agent_turn"]).run_agent_turn(
                adapter, inbound, agent_name="sam"
            )

        assert result == "Hello from Sam!"
        # Container name from FAKE_AGENT_MAP["sam"]
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == "sam-container"

    @pytest.mark.asyncio
    async def test_mention_resolves_agent(self):
        """@lisa in the message text routes to the lisa agent."""
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("@lisa please review")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound)

        assert mock_run.call_args[0][0] == "lisa-container"

    @pytest.mark.asyncio
    async def test_no_mention_defaults_to_sam(self):
        """No @mention and no explicit override routes to the default agent (sam)."""
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("what is 2+2?")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound)

        assert mock_run.call_args[0][0] == "sam-container"

    @pytest.mark.asyncio
    async def test_unknown_agent_raises_value_error(self):
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
        ):
            from router.chat.core import run_agent_turn

            with pytest.raises(ValueError, match="Unknown agent"):
                await run_agent_turn(adapter, inbound, agent_name="nobody")


# ---------------------------------------------------------------------------
# run_agent_turn — adapter integration
# ---------------------------------------------------------------------------


class TestRunAgentTurnAdapterIntegration:
    @pytest.mark.asyncio
    async def test_status_thinking_emitted(self):
        """set_status(THINKING) must be called before the CLI invocation."""
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        assert "..." in out.getvalue()  # THINKING label from TerminalAdapter

    @pytest.mark.asyncio
    async def test_status_done_emitted(self):
        """set_status(DONE) must be called after a successful turn."""
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        assert "done" in out.getvalue()  # DONE label from TerminalAdapter

    @pytest.mark.asyncio
    async def test_reply_delivered_via_send_message(self):
        """The agent reply must be delivered via adapter.send_message."""
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            result = await run_agent_turn(adapter, inbound, agent_name="sam")

        assert result == "Hello from Sam!"
        assert "Hello from Sam!" in out.getvalue()

    @pytest.mark.asyncio
    async def test_thread_history_loaded_via_adapter(self):
        """Thread history must come from adapter.read_thread, not Slack."""
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InboundMessage

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("sess-hist")
        principal = adapter.resolve_principal("alice")

        # Pre-populate one history message
        prior = InboundMessage(conversation_ref=ref, principal_ref=principal, text="earlier message")
        adapter.record_inbound(prior)

        current = InboundMessage(conversation_ref=ref, principal_ref=principal, text="current")

        captured_context: list[str] = []

        async def capturing_run(container, cmd, timeout, *, stdin_data=None, env=None):
            if stdin_data:
                captured_context.append(stdin_data)
            return (_GOOD_JSON, "", 0)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=capturing_run),
        ):
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, current, agent_name="sam")

        assert captured_context, "Expected context to be passed to _run_in_container"
        assert "earlier message" in captured_context[0]

    @pytest.mark.asyncio
    async def test_no_slack_client_reference(self):
        """run_agent_turn must not import or use the Slack WebClient."""
        import router.chat.core as core_module

        source = __import__("inspect").getsource(core_module)
        assert "WebClient" not in source
        assert "slack_sdk" not in source
        assert "slack_bolt" not in source

    @pytest.mark.asyncio
    async def test_reply_uses_conversation_ref(self):
        """The outbound message must carry the same conversation_ref as the inbound."""
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InboundMessage, OutboundMessage

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("my-session")
        principal = adapter.resolve_principal("bob")
        inbound = InboundMessage(conversation_ref=ref, principal_ref=principal, text="hi")

        sent: list[OutboundMessage] = []
        original_send = adapter.send_message

        async def capturing_send(outbound: OutboundMessage) -> None:
            sent.append(outbound)
            await original_send(outbound)

        adapter.send_message = capturing_send  # type: ignore[method-assign]

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        # Filter to the reply (the status calls go through set_status, not send_message)
        replies = [m for m in sent if m.conversation_ref is not None]
        assert replies, "Expected a reply with a conversation_ref"
        assert replies[0].conversation_ref == ref


# ---------------------------------------------------------------------------
# run_agent_turn — error handling
# ---------------------------------------------------------------------------


class TestRunAgentTurnErrors:
    @pytest.mark.asyncio
    async def test_non_zero_exit_raises_dispatch_error(self):
        from router.dispatcher import DispatchError

        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = ("", "some error", 1)
            from router.chat.core import run_agent_turn

            with pytest.raises(DispatchError):
                await run_agent_turn(adapter, inbound, agent_name="sam")

    @pytest.mark.asyncio
    async def test_empty_stdout_raises_dispatch_error(self):
        from router.dispatcher import DispatchError

        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = ("", "", 0)
            from router.chat.core import run_agent_turn

            with pytest.raises(DispatchError, match="empty response"):
                await run_agent_turn(adapter, inbound, agent_name="sam")

    @pytest.mark.asyncio
    async def test_invalid_json_raises_dispatch_error(self):
        from router.dispatcher import DispatchError

        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = ("not-json", "", 0)
            from router.chat.core import run_agent_turn

            with pytest.raises(DispatchError, match="invalid JSON"):
                await run_agent_turn(adapter, inbound, agent_name="sam")

    @pytest.mark.asyncio
    async def test_empty_result_field_raises_dispatch_error(self):
        from router.dispatcher import DispatchError

        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (json.dumps({"result": ""}), "", 0)
            from router.chat.core import run_agent_turn

            with pytest.raises(DispatchError, match="empty result"):
                await run_agent_turn(adapter, inbound, agent_name="sam")


# ---------------------------------------------------------------------------
# run_agent_turn — CLI command construction
# ---------------------------------------------------------------------------


class TestRunAgentTurnCLICommand:
    @pytest.mark.asyncio
    async def test_cli_command_contains_claude(self):
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        _container, cmd, _timeout = mock_run.call_args[0]
        assert cmd[0] == "claude"

    @pytest.mark.asyncio
    async def test_cli_command_output_format_json(self):
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        _container, cmd, _timeout = mock_run.call_args[0]
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    @pytest.mark.asyncio
    async def test_model_flag_added_when_configured(self):
        agent_map_with_model = {
            "sam": {
                "container": "sam-container",
                "name": "Sam",
                "model": "claude-sonnet-4-6",
                "container_timeout": None,
            }
        }
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=agent_map_with_model),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        _container, cmd, _timeout = mock_run.call_args[0]
        assert "--model" in cmd
        assert "claude-sonnet-4-6" in cmd

    @pytest.mark.asyncio
    async def test_model_flag_absent_when_not_configured(self):
        out = io.StringIO()
        adapter = _make_adapter(output=out)
        inbound = _make_inbound("hi")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = (_GOOD_JSON, "", 0)
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, inbound, agent_name="sam")

        _container, cmd, _timeout = mock_run.call_args[0]
        assert "--model" not in cmd


# ---------------------------------------------------------------------------
# terminal_console — structure
# ---------------------------------------------------------------------------


class TestTerminalConsoleStructure:
    def test_has_redirect_router_logs_to_stderr(self):
        from router.chat.terminal_console import _redirect_router_logs_to_stderr  # noqa: F401

    def test_has_console_loop(self):
        from router.chat.terminal_console import _console_loop  # noqa: F401

    def test_main_is_callable(self):
        from router.chat.terminal_console import main

        assert callable(main)

    def test_module_has_dunder_main_guard(self):
        import router.chat.terminal_console as tc

        source = __import__("inspect").getsource(tc)
        assert '__name__ == "__main__"' in source

    def test_no_slack_references(self):
        import router.chat.terminal_console as tc

        source = __import__("inspect").getsource(tc)
        assert "WebClient" not in source
        assert "slack_sdk" not in source
        assert "slack_bolt" not in source


# ---------------------------------------------------------------------------
# terminal_console — no duplicate message in context (issue #568)
# ---------------------------------------------------------------------------


class TestNoMessageDuplication:
    """Regression tests for #568: current message must appear exactly once.

    The fix is to call record_inbound AFTER run_agent_turn, so the message is
    absent from read_thread() when the context is built and reaches the agent
    only via new_message in build_full_context.
    """

    @pytest.mark.asyncio
    async def test_current_message_appears_once_in_context(self):
        """New message must not be double-counted (once in history, once as new_message)."""
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InboundMessage

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("dedup-session")
        principal = adapter.resolve_principal("tester")
        inbound = InboundMessage(conversation_ref=ref, principal_ref=principal, text="xyzzy-dedup-needle-42")

        captured: list[str] = []

        async def capturing_run(container, cmd, timeout, *, stdin_data=None, env=None):
            if stdin_data:
                captured.append(stdin_data)
            return (_GOOD_JSON, "", 0)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=capturing_run),
        ):
            from router.chat.core import run_agent_turn

            # Fixed order: run first (history does not yet contain inbound),
            # then record. Calling record_inbound before run_agent_turn would
            # cause the message to appear twice — this test would catch that.
            await run_agent_turn(adapter, inbound, agent_name="sam")

        assert captured, "Expected context to be captured"
        count = captured[0].count("xyzzy-dedup-needle-42")
        assert count == 1, f"Message appeared {count} time(s) in context; expected exactly 1"

    @pytest.mark.asyncio
    async def test_prior_history_still_included(self):
        """Existing prior messages must still reach the agent via thread history."""
        from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
        from router.chat.types import InboundMessage

        out = io.StringIO()
        adapter = TerminalAdapter(output=out)
        ref = make_inbound_ref("hist-session")
        principal = adapter.resolve_principal("tester")

        # Simulate one already-completed turn: record a prior message.
        prior = InboundMessage(conversation_ref=ref, principal_ref=principal, text="earlier-turn-text")
        adapter.record_inbound(prior)

        current = InboundMessage(conversation_ref=ref, principal_ref=principal, text="current-turn-text")

        captured: list[str] = []

        async def capturing_run(container, cmd, timeout, *, stdin_data=None, env=None):
            if stdin_data:
                captured.append(stdin_data)
            return (_GOOD_JSON, "", 0)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=capturing_run),
        ):
            from router.chat.core import run_agent_turn

            await run_agent_turn(adapter, current, agent_name="sam")

        assert captured, "Expected context to be captured"
        context = captured[0]
        assert "earlier-turn-text" in context, "Prior message must appear in history"
        assert context.count("current-turn-text") == 1, "Current message must appear exactly once"

    @pytest.mark.asyncio
    async def test_console_loop_no_message_duplication(self):
        """_console_loop must call record_inbound after run_agent_turn, not before."""
        from router.chat.terminal_console import _console_loop

        captured: list[str] = []

        async def capturing_run(container, cmd, timeout, *, stdin_data=None, env=None):
            if stdin_data:
                captured.append(stdin_data)
            return (_GOOD_JSON, "", 0)

        fake_stdin = io.StringIO("console-loop-needle-99\n")

        with (
            patch("sys.stdin", fake_stdin),
            patch("sys.stdout", io.StringIO()),
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=capturing_run),
        ):
            await _console_loop("sam")

        assert captured, "Expected context to be captured via _console_loop"
        count = captured[0].count("console-loop-needle-99")
        assert count == 1, f"_console_loop: message appeared {count} time(s), expected 1"
