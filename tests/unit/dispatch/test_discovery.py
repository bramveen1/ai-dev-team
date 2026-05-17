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


def _seed_launched(root: str, dispatch_id: str, *, agent: str = "sam") -> None:
    """Mimic packs.dispatch.handler.dispatch_issue's write order."""
    dstate.write_field(dispatch_id, dstate.FIELD_STARTED_AT, "2026-05-17T12:00:00+00:00", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_BUDGET, "1800", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_CHANNEL, "C1", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_THREAD_TS, "1.0", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_AGENT, agent, root=root)
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

    def test_skips_terminal_dispatch(self, store, root):
        _seed_launched(root, "disp-done")
        dstate.write_field("disp-done", dstate.FIELD_EXITCODE, "0", root=root)
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
