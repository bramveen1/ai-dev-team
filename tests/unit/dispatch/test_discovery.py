"""Unit tests for router.dispatch.discovery — the registration reconciler.

The discovery loop is the production wiring that was missing in the
first cut of #163: it watches /var/lib/dispatch/ for launched-but-
unsupervised dispatches and registers the supervision system task.
These tests pin the idempotency, skip rules, and resolver wiring.
"""

from __future__ import annotations

import asyncio

import pytest

from router.dispatch import discovery, supervision
from router.dispatch import state as dstate
from router.scheduled_tasks.store import ScheduledTaskStore

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "dispatches")


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


def _seed_launched(root: str, dispatch_id: str, *, agent: str = "sam", mode: str = "poll") -> None:
    """Mimic packs.dispatch.handler.dispatch_issue's write order."""
    dstate.write_field(dispatch_id, dstate.FIELD_STARTED_AT, "2026-05-17T12:00:00+00:00", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_BUDGET, "1800", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_CHANNEL, "C1", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_THREAD_TS, "1.0", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_AGENT, agent, root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_SUPERVISION_MODE, mode, root=root)
    # ``pid`` is the launch sentinel — the handler writes it last.
    dstate.write_field(dispatch_id, dstate.FIELD_PID, "12345", root=root)


class TestReconcileOnce:
    def test_registers_launched_unsupervised_dispatch(self, store, root):
        _seed_launched(root, "disp-a")
        registered = discovery.reconcile_once(store, root=root)
        assert registered == ["disp-a"]

        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert len(tasks) == 1
        assert tasks[0].payload["dispatch_id"] == "disp-a"
        assert tasks[0].payload["channel"] == "C1"
        assert tasks[0].agent_name == "sam"

    def test_registers_discord_transport_ref_into_payload(self, store, root):
        """#713: transport/conversation_id sidecar fields, when present, flow
        into the supervision task payload so check_dispatch can resolve a
        ChatAdapter for the dispatch."""
        _seed_launched(root, "disp-discord")
        dstate.write_field("disp-discord", dstate.FIELD_TRANSPORT, "discord", root=root)
        dstate.write_field("disp-discord", dstate.FIELD_CONVERSATION_ID, "discord:1:2:3", root=root)

        registered = discovery.reconcile_once(store, root=root)
        assert registered == ["disp-discord"]

        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert tasks[0].payload["transport"] == "discord"
        assert tasks[0].payload["conversation_id"] == "discord:1:2:3"

    def test_slack_dispatch_has_no_transport_ref_in_payload(self, store, root):
        """A pre-#713 (or Slack) dispatch dir has no transport/conversation_id
        sidecar files — the payload must not grow spurious empty keys."""
        _seed_launched(root, "disp-a")
        discovery.reconcile_once(store, root=root)
        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert "transport" not in tasks[0].payload
        assert "conversation_id" not in tasks[0].payload

    def test_idempotent_second_call_registers_nothing(self, store, root):
        _seed_launched(root, "disp-a")
        discovery.reconcile_once(store, root=root)
        again = discovery.reconcile_once(store, root=root)
        assert again == []
        assert len(store.list_by_callable_ref(supervision.CALLABLE_REF)) == 1

    def test_skips_dispatch_without_pid(self, store, root):
        # Half-written launch dir: started_at but no pid yet.
        dstate.write_field("disp-half", dstate.FIELD_STARTED_AT, "2026-05-17T12:00:00+00:00", root=root)
        dstate.write_field("disp-half", dstate.FIELD_AGENT, "sam", root=root)
        registered = discovery.reconcile_once(store, root=root)
        assert registered == []

    def test_backfills_terminal_unsupervised_poll_dispatch(self, store, root):
        """Issue #705: a poll dispatch that went terminal with no supervisor
        ever registered (discovery was down for its whole lifetime) must
        still get registered so the next tick posts its terminal message —
        not silently dropped forever."""
        _seed_launched(root, "disp-done", mode="poll")
        dstate.write_field("disp-done", dstate.FIELD_EXITCODE, "0", root=root)
        registered = discovery.reconcile_once(store, root=root)
        assert registered == ["disp-done"]
        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert len(tasks) == 1
        assert tasks[0].payload["dispatch_id"] == "disp-done"

    def test_does_not_backfill_already_posted_terminal_dispatch(self, store, root):
        """The common case: a poll dispatch that completed normally already
        had its terminal message posted and its supervision task
        deregistered. The terminal_posted marker must stop discovery from
        treating that as an orphan and double-posting."""
        _seed_launched(root, "disp-done", mode="poll")
        dstate.write_field("disp-done", dstate.FIELD_EXITCODE, "0", root=root)
        (dstate.dispatch_dir("disp-done", root=root) / dstate.FIELD_TERMINAL_POSTED).touch()
        registered = discovery.reconcile_once(store, root=root)
        assert registered == []

    def test_does_not_backfill_inline_terminal_dispatch(self, store, root):
        """Inline-mode dispatches report synchronously and are never meant
        to be supervised — backfilling one would double-post."""
        _seed_launched(root, "disp-inline", mode="inline")
        dstate.write_field("disp-inline", dstate.FIELD_EXITCODE, "0", root=root)
        registered = discovery.reconcile_once(store, root=root)
        assert registered == []

    def test_does_not_backfill_terminal_dispatch_missing_supervision_mode(self, store, root):
        """Pre-#705 dispatch dirs have no supervision_mode file at all —
        treat that as unknown/inline rather than assuming poll."""
        dstate.write_field("disp-legacy", dstate.FIELD_STARTED_AT, "2026-05-17T12:00:00+00:00", root=root)
        dstate.write_field("disp-legacy", dstate.FIELD_AGENT, "sam", root=root)
        dstate.write_field("disp-legacy", dstate.FIELD_PID, "12345", root=root)
        dstate.write_field("disp-legacy", dstate.FIELD_EXITCODE, "0", root=root)
        registered = discovery.reconcile_once(store, root=root)
        assert registered == []

    def test_skips_halted_dispatch(self, store, root):
        _seed_launched(root, "disp-halt")
        dstate.write_field("disp-halt", dstate.FIELD_HALT_MARKER, "now", root=root)
        registered = discovery.reconcile_once(store, root=root)
        assert registered == []

    def test_skips_dispatch_missing_agent_file(self, store, root, caplog):
        # Write pid + started_at but no agent file. Should warn + skip
        # rather than register a row with empty agent_name (which would
        # break ScopedStore queries).
        dstate.write_field("disp-noagent", dstate.FIELD_STARTED_AT, "2026-05-17T12:00:00+00:00", root=root)
        dstate.write_field("disp-noagent", dstate.FIELD_PID, "999", root=root)
        registered = discovery.reconcile_once(store, root=root)
        assert registered == []

    def test_uses_resolver_to_populate_agent_user_id(self, store, root):
        _seed_launched(root, "disp-a", agent="sam")

        def resolver(name: str) -> str | None:
            return "U_SAM" if name == "sam" else None

        discovery.reconcile_once(store, agent_user_id_resolver=resolver, root=root)
        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert tasks[0].payload["agent_user_id"] == "U_SAM"

    def test_multiple_dispatches_each_registered(self, store, root):
        _seed_launched(root, "disp-a")
        _seed_launched(root, "disp-b", agent="lisa")
        registered = discovery.reconcile_once(store, root=root)
        assert set(registered) == {"disp-a", "disp-b"}
        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert len(tasks) == 2
        agents = {t.agent_name for t in tasks}
        assert agents == {"sam", "lisa"}

    def test_register_error_is_isolated(self, store, root, monkeypatch):
        """A failure registering one dispatch must not stop the others."""
        _seed_launched(root, "disp-a")
        _seed_launched(root, "disp-b")

        original = supervision.register_supervision
        call_count = {"n": 0}

        def flaky(store_arg, **kwargs):
            call_count["n"] += 1
            if kwargs.get("dispatch_id") == "disp-a":
                raise RuntimeError("transient")
            return original(store_arg, **kwargs)

        monkeypatch.setattr(discovery, "register_supervision", flaky)

        registered = discovery.reconcile_once(store, root=root)
        # disp-a failed; disp-b still registered.
        assert registered == ["disp-b"]
        assert call_count["n"] == 2


class TestRunForever:
    @pytest.mark.asyncio
    async def test_stop_event_terminates_loop(self, store, root):
        stop = asyncio.Event()
        task = asyncio.create_task(discovery.run_forever(store, interval_seconds=0.05, root=root, stop_event=stop))
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_loop_picks_up_newly_launched_dispatches(self, store, root):
        stop = asyncio.Event()
        task = asyncio.create_task(discovery.run_forever(store, interval_seconds=0.02, root=root, stop_event=stop))
        # Seed a dispatch after the loop is already running.
        await asyncio.sleep(0.01)
        _seed_launched(root, "disp-late")
        # Give the loop a couple of ticks to notice.
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

        tasks = store.list_by_callable_ref(supervision.CALLABLE_REF)
        assert any(t.payload["dispatch_id"] == "disp-late" for t in tasks)

    @pytest.mark.asyncio
    async def test_loop_swallows_errors_and_keeps_running(self, store, root, monkeypatch):
        calls = {"n": 0}

        def flaky(store_arg, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first tick boom")

        monkeypatch.setattr(discovery, "reconcile_once", flaky)

        stop = asyncio.Event()
        task = asyncio.create_task(discovery.run_forever(store, interval_seconds=0.02, root=root, stop_event=stop))
        await asyncio.sleep(0.08)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert calls["n"] >= 2

    @pytest.mark.asyncio
    async def test_tick_emits_heartbeat_even_when_nothing_registered(self, store, root, caplog):
        """Issue #705: a quiet tick (nothing to register) must still log,
        so a hung/dead loop is distinguishable from an idle one instead of
        both looking like silence."""
        stop = asyncio.Event()
        with caplog.at_level("INFO", logger="router.dispatch.discovery"):
            task = asyncio.create_task(discovery.run_forever(store, interval_seconds=0.02, root=root, stop_event=stop))
            await asyncio.sleep(0.07)
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)

        heartbeats = [r for r in caplog.records if "Dispatch discovery tick" in r.message]
        assert len(heartbeats) >= 2, [r.message for r in caplog.records]


class TestRunForeverResilient:
    @pytest.mark.asyncio
    async def test_restarts_after_crash_and_logs_critical(self, store, root, monkeypatch, caplog):
        """Issue #705: if the loop dies from something run_forever's own
        try/except doesn't cover, it must restart (loudly) rather than
        vanish forever with nothing but a startup log line."""
        monkeypatch.setattr(discovery, "_RESTART_BACKOFF_SECONDS", 0.01)

        calls = {"n": 0}
        real_run_forever = discovery.run_forever

        async def flaky_run_forever(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash")
            return await real_run_forever(*args, **kwargs)

        monkeypatch.setattr(discovery, "run_forever", flaky_run_forever)

        with caplog.at_level("CRITICAL", logger="router.dispatch.discovery"):
            task = asyncio.create_task(discovery._run_forever_resilient(store, interval_seconds=0.02, root=root))
            # Let it crash once and restart before we cancel the (restarted)
            # inner loop.
            await asyncio.sleep(0.05)

        assert calls["n"] >= 2, "expected the crashed loop to be restarted"
        assert any("crashed" in r.message for r in caplog.records), [r.message for r in caplog.records]

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_cancellation_propagates_without_restart(self, store, root, monkeypatch):
        """A real shutdown (task.cancel()) must stop the loop for good, not
        get treated as a crash and restarted."""
        monkeypatch.setattr(discovery, "_RESTART_BACKOFF_SECONDS", 0.01)

        task = asyncio.create_task(discovery._run_forever_resilient(store, interval_seconds=10, root=root))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
