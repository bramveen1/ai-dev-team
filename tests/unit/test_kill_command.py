"""Unit tests for router.kill_command — the ``/kill`` slash command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from router.kill_command import _parse_kill_args, handle_kill_command
from router.stuck_guard import (
    MODE_DRY_RUN,
    MODE_ENFORCE,
    GuardConfig,
    StuckGuard,
    make_task_id,
)

pytestmark = pytest.mark.unit


# ── _parse_kill_args ──────────────────────────────────────────────────


class TestParseKillArgs:
    def test_empty(self):
        assert _parse_kill_args("") == (None, False)

    def test_whitespace(self):
        assert _parse_kill_args("   ") == (None, False)

    def test_agent_only(self):
        assert _parse_kill_args("sam") == ("sam", False)

    def test_agent_lowercased(self):
        assert _parse_kill_args("SAM") == ("sam", False)

    def test_all_threads(self):
        assert _parse_kill_args("sam all") == ("sam", True)

    def test_extra_args_ignored(self):
        assert _parse_kill_args("sam ALL extra") == ("sam", True)


# ── handle_kill_command ───────────────────────────────────────────────


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture
def respond():
    return AsyncMock()


@pytest.fixture
def ack():
    return AsyncMock()


def _body(text: str = "", channel: str = "C123", thread_ts: str = "1234.5") -> dict:
    return {
        "text": text,
        "channel_id": channel,
        "thread_ts": thread_ts,
        "user_id": "U_op",
    }


class TestHandleKillCommand:
    @pytest.mark.asyncio
    async def test_kills_named_agent_in_current_thread(self, ack, respond, mock_client):
        # Acceptance: "/kill sam in Slack stops Sam mid-turn within one
        # dispatch cycle." → set the task halted.
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        body = _body("sam", channel="C1", thread_ts="100.0")

        # `sam` must be a known agent; we patch the agent map.
        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {"name": "Sam"}, "lisa": {"name": "Lisa"}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        ack.assert_awaited_once()
        respond.assert_awaited()
        task_id = make_task_id("C1", "100.0", "sam")
        assert guard.is_halted(task_id)

    @pytest.mark.asyncio
    async def test_kill_works_in_dry_run(self, ack, respond, mock_client):
        # Acceptance: "Kill works in both dry-run and enforce mode."
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        body = _body("lisa", channel="C1", thread_ts="100.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"lisa": {"name": "Lisa"}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        assert guard.is_halted(make_task_id("C1", "100.0", "lisa"))

    @pytest.mark.asyncio
    async def test_kill_works_in_enforce(self, ack, respond, mock_client):
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE))
        body = _body("lisa", channel="C1", thread_ts="100.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"lisa": {"name": "Lisa"}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        assert guard.is_halted(make_task_id("C1", "100.0", "lisa"))

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_friendly_error(self, ack, respond, mock_client):
        guard = StuckGuard()
        body = _body("nonexistent")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"lisa": {}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        respond.assert_awaited()
        msg = respond.await_args.kwargs.get("text") or respond.await_args.args[0]
        assert "Unknown" in msg or "unknown" in msg

    @pytest.mark.asyncio
    async def test_no_agent_specified_uses_resolver(self, ack, respond, mock_client):
        guard = StuckGuard()
        body = _body("", channel="C1", thread_ts="100.0")

        resolver = MagicMock(return_value="lisa")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"lisa": {}})
            await handle_kill_command(
                ack=ack,
                body=body,
                respond=respond,
                client=mock_client,
                guard=guard,
                active_agent_resolver=resolver,
            )

        resolver.assert_called_once_with("C1", "100.0")
        assert guard.is_halted(make_task_id("C1", "100.0", "lisa"))

    @pytest.mark.asyncio
    async def test_no_agent_no_resolver_returns_error(self, ack, respond, mock_client):
        guard = StuckGuard()
        body = _body("", channel="C1", thread_ts="100.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"lisa": {}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        respond.assert_awaited()

    @pytest.mark.asyncio
    async def test_all_kills_every_task_for_agent(self, ack, respond, mock_client):
        guard = StuckGuard()
        # Seed three tasks for sam across two threads, and one for lisa
        guard.record_turn(task_id=make_task_id("C1", "1.0", "sam"), agent_name="sam")
        guard.record_turn(task_id=make_task_id("C2", "2.0", "sam"), agent_name="sam")
        guard.record_turn(task_id=make_task_id("C1", "1.0", "lisa"), agent_name="lisa")

        body = _body("sam all", channel="C1", thread_ts="1.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}, "lisa": {}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        # Both sam tasks halted; lisa untouched.
        assert guard.is_halted(make_task_id("C1", "1.0", "sam"))
        assert guard.is_halted(make_task_id("C2", "2.0", "sam"))
        assert not guard.is_halted(make_task_id("C1", "1.0", "lisa"))

    @pytest.mark.asyncio
    async def test_post_mortem_written_for_kill(self, ack, respond, mock_client, tmp_path):
        # Acceptance: "Kill writes a post-mortem tagged reason: manual_kill."
        guard = StuckGuard(GuardConfig(post_mortem_dir=str(tmp_path)))
        body = _body("lisa", channel="C1", thread_ts="100.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"lisa": {}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        files = list(tmp_path.iterdir())
        assert files, "expected at least one post-mortem file"
        contents = files[0].read_text()
        assert "manual_kill" in contents


class TestHaltMarkerIntegration:
    """``/kill`` writes a halt_marker into every in-flight dispatch (#163)."""

    @pytest.mark.asyncio
    async def test_kill_drops_halt_marker_for_active_dispatch(self, ack, respond, mock_client, tmp_path):
        from router.dispatch import state as dstate

        # Seed two dispatches: one owned by sam (should be halted), one
        # owned by lisa (should be left alone).
        dstate.write_field("disp-sam", dstate.FIELD_AGENT, "sam", root=str(tmp_path))
        dstate.write_field("disp-sam", dstate.FIELD_PID, "12345", root=str(tmp_path))
        dstate.write_field("disp-lisa", dstate.FIELD_AGENT, "lisa", root=str(tmp_path))
        dstate.write_field("disp-lisa", dstate.FIELD_PID, "67890", root=str(tmp_path))

        guard = StuckGuard()
        body = _body("sam", channel="C1", thread_ts="1.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}, "lisa": {}})
            mp.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        # Halt marker exists for sam's dispatch but not lisa's.
        assert dstate.read_field("disp-sam", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is not None
        assert dstate.read_field("disp-lisa", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is None

        # The ack summary mentions the halted dispatch count.
        summary_text = respond.await_args.kwargs.get("text") or respond.await_args.args[0]
        assert "dispatch" in summary_text

    @pytest.mark.asyncio
    async def test_kill_all_with_no_dispatch_and_no_task_says_no_active(self, ack, respond, mock_client, tmp_path):
        # No dispatches seeded, no live task in the guard registry.
        # Use the ``all`` variant — the single-thread path always
        # synthesizes a task id for the named (channel, thread, agent),
        # so the empty-result branch only triggers under broadcast kill.
        guard = StuckGuard()
        body = _body("sam all", channel="C1", thread_ts="1.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}})
            mp.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        summary_text = respond.await_args.kwargs.get("text") or respond.await_args.args[0]
        assert "No active task" in summary_text

    @pytest.mark.asyncio
    async def test_kill_all_with_dispatch_only_still_reports_halted(self, ack, respond, mock_client, tmp_path):
        """A dispatch exists but no guard task — kill should still halt the dispatch."""
        from router.dispatch import state as dstate

        dstate.write_field("disp-sam", dstate.FIELD_AGENT, "sam", root=str(tmp_path))

        guard = StuckGuard()
        body = _body("sam all")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}})
            mp.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        assert dstate.read_field("disp-sam", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is not None
        summary_text = respond.await_args.kwargs.get("text") or respond.await_args.args[0]
        assert "dispatch" in summary_text
