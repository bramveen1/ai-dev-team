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


@pytest.fixture(autouse=True)
def _isolate_dispatch_root(tmp_path, monkeypatch):
    """Sandbox ``DISPATCH_WORKSPACE_ROOT`` for every test in this module.

    ``handle_kill_command`` calls ``mark_halted_for_agent`` without an
    explicit ``root`` argument, which falls back to
    ``$DISPATCH_WORKSPACE_ROOT`` and then to the production default
    ``/var/lib/dispatch``. Without this fixture, any test that drives the
    kill path writes ``halt_marker`` files into the *live* dispatch root —
    and if pytest is itself running inside an agent dispatch sandbox
    (e.g. a worker doing ``pytest tests/unit/``), it halts its own
    sibling dispatches, including the worker running the test.

    Regression for two real incidents on 2026-06-07:
      * 09:57 — sam dispatch self-killed via
        ``test_all_kills_every_task_for_agent`` (post-mortem
        ``sam-20260607T100957Z.md``).
      * 13:32 — repeat self-kill (``dispatch-20260607T133211-267fcf``)
        when the #274 worker re-ran the same suite for baselining.

    ``TestHaltMarkerIntegration`` tests set ``DISPATCH_WORKSPACE_ROOT``
    inside their own ``MonkeyPatch().context()``; that nested setenv
    transparently overrides this fixture and is restored when the inner
    context exits.
    """
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))


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

        # Seed two dispatches: one owned by sam in this thread (should be
        # halted), one owned by lisa (should be left alone).
        dstate.write_field("disp-sam", dstate.FIELD_AGENT, "sam", root=str(tmp_path))
        dstate.write_field("disp-sam", dstate.FIELD_PID, "12345", root=str(tmp_path))
        dstate.write_field("disp-sam", dstate.FIELD_CHANNEL, "C1", root=str(tmp_path))
        dstate.write_field("disp-sam", dstate.FIELD_THREAD_TS, "1.0", root=str(tmp_path))
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
    async def test_thread_scoped_kill_spares_sibling_dispatch_in_other_thread(
        self, ack, respond, mock_client, tmp_path
    ):
        """Regression for #256: a bare ``/kill sam`` must not SIGTERM a healthy
        sam dispatch running in a *different* thread."""
        from router.dispatch import state as dstate

        # Two in-flight sam dispatches in different threads.
        for did, ch, th in (("disp-stuck", "C1", "1.0"), ("disp-healthy", "C2", "2.0")):
            dstate.write_field(did, dstate.FIELD_AGENT, "sam", root=str(tmp_path))
            dstate.write_field(did, dstate.FIELD_PID, "111", root=str(tmp_path))
            dstate.write_field(did, dstate.FIELD_CHANNEL, ch, root=str(tmp_path))
            dstate.write_field(did, dstate.FIELD_THREAD_TS, th, root=str(tmp_path))

        guard = StuckGuard()
        # Operator kills sam from the stuck dispatch's thread only.
        body = _body("sam", channel="C1", thread_ts="1.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}})
            mp.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        # Only the in-thread dispatch is halted; the sibling is untouched.
        assert dstate.read_field("disp-stuck", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is not None
        assert dstate.read_field("disp-healthy", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_kill_all_halts_sibling_dispatches_across_threads(self, ack, respond, mock_client, tmp_path):
        """``/kill sam all`` is the explicit broadcast escape hatch — it halts
        every sam dispatch regardless of thread."""
        from router.dispatch import state as dstate

        for did, ch, th in (("disp-a", "C1", "1.0"), ("disp-b", "C2", "2.0")):
            dstate.write_field(did, dstate.FIELD_AGENT, "sam", root=str(tmp_path))
            dstate.write_field(did, dstate.FIELD_PID, "111", root=str(tmp_path))
            dstate.write_field(did, dstate.FIELD_CHANNEL, ch, root=str(tmp_path))
            dstate.write_field(did, dstate.FIELD_THREAD_TS, th, root=str(tmp_path))

        guard = StuckGuard()
        body = _body("sam all", channel="C1", thread_ts="1.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}})
            mp.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        assert dstate.read_field("disp-a", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is not None
        assert dstate.read_field("disp-b", dstate.FIELD_HALT_MARKER, root=str(tmp_path)) is not None

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
    async def test_kill_does_not_bleed_into_default_dispatch_root(self, ack, respond, mock_client):
        """Regression: kill_command tests must never write into the live
        ``/var/lib/dispatch`` root (incidents 2026-06-07 09:57 and 13:32).

        Forces the worst-case kill path (``sam all``) without seeding any
        dispatches and asserts that the autouse fixture has set
        ``DISPATCH_WORKSPACE_ROOT`` away from the production default. If
        someone removes the fixture, this test starts failing before any
        live-state bleed can occur.
        """
        import os

        from router.dispatch.state import DEFAULT_DISPATCH_ROOT

        active_root = os.environ.get("DISPATCH_WORKSPACE_ROOT")
        assert active_root is not None, "autouse fixture must set DISPATCH_WORKSPACE_ROOT"
        assert active_root != DEFAULT_DISPATCH_ROOT, (
            "DISPATCH_WORKSPACE_ROOT must be sandboxed away from the live dispatch root"
        )

        guard = StuckGuard()
        body = _body("sam all", channel="C1", thread_ts="1.0")

        from router import kill_command as kc

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(kc, "get_agent_map", lambda: {"sam": {}})
            await handle_kill_command(ack=ack, body=body, respond=respond, client=mock_client, guard=guard)

        # Even after the kill path ran, the env still points at the sandbox
        # — and no test-side seeding happened in the live root.
        assert os.environ.get("DISPATCH_WORKSPACE_ROOT") == active_root

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
