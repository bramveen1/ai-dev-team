"""Unit tests for router.dispatch.attachments_sweep — the periodic GC system task (#327).

Covers the router-side wiring that the pack-side janitor can't provide:
the sweep must run in the router process (RW mount), be registered as a
singleton system task, and never crash the scheduler loop.
"""

from __future__ import annotations

import pytest

from router.dispatch import attachments_sweep as ats
from router.scheduled_tasks.store import ScheduledTaskStore

pytestmark = pytest.mark.unit


# --- run_sweep: scheduler-callable contract --------------------------------


def test_run_sweep_accepts_scheduler_kwargs_and_returns_non_terminal(monkeypatch):
    """run_sweep must accept (payload, slack_client, now) and never return done."""
    summary = {"removed": 2, "kept": 1, "errors": 0, "bytes_freed": 99}
    monkeypatch.setattr(ats, "_load_sweep_fn", lambda: lambda: summary)
    result = ats.run_sweep(payload={}, slack_client=object(), now=None)
    assert result["status"] == "ok"
    # Crucial: must NOT be terminal, or the scheduler deregisters the singleton.
    assert result["status"] != "done"
    # Sweep summary is merged through for log/telemetry visibility.
    assert result["removed"] == 2
    assert result["bytes_freed"] == 99


def test_run_sweep_swallows_sweep_exception(monkeypatch):
    """A throwing sweep keeps the task scheduled rather than killing the loop."""

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(ats, "_load_sweep_fn", lambda: _boom)
    result = ats.run_sweep()
    assert result["status"] == "ok"
    assert result["error"] == "sweep_failed"


def test_run_sweep_handles_unavailable_pack(monkeypatch):
    """A missing/partial pack degrades to a no-op, not a crash."""
    monkeypatch.setattr(ats, "_load_sweep_fn", lambda: None)
    result = ats.run_sweep()
    assert result["status"] == "ok"
    assert result["skipped"] == "pack_unavailable"


def test_load_sweep_fn_imports_real_pack_and_sweeps(tmp_path):
    """The dynamic pack import resolves the real attachments_janitor.sweep."""
    sweep = ats._load_sweep_fn()
    assert sweep is not None
    # Old thread dir gets reaped; the real pack logic runs end-to-end.
    old = tmp_path / "T_old"
    old.mkdir()
    import os

    os.utime(old, (0, 0))  # mtime = epoch → far older than TTL
    result = sweep(attachments_root=str(tmp_path))
    assert result["removed"] == 1
    assert not old.exists()


# --- register_attachments_sweep: idempotent singleton ----------------------


def test_register_creates_singleton_system_task(tmp_path):
    store = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    task = ats.register_attachments_sweep(store, agent_name="sam")
    assert task.callable_ref == ats.CALLABLE_REF
    assert task.period_seconds == ats.SWEEP_PERIOD_SECONDS
    assert task.is_system_task
    assert task.enabled
    rows = store.list_by_callable_ref(ats.CALLABLE_REF)
    assert len(rows) == 1


def test_register_is_idempotent_across_restarts(tmp_path):
    """Second boot must not create a duplicate sweep task."""
    db = str(tmp_path / "tasks.db")
    first = ats.register_attachments_sweep(ScheduledTaskStore(db), agent_name="sam")
    # Simulate a router restart re-opening the same persisted store.
    second = ats.register_attachments_sweep(ScheduledTaskStore(db), agent_name="sam")
    assert second.task_id == first.task_id
    assert len(ScheduledTaskStore(db).list_by_callable_ref(ats.CALLABLE_REF)) == 1


def test_callable_ref_is_importable():
    """The dotted callable_ref the scheduler imports must resolve to run_sweep."""
    module_name, _, attr = ats.CALLABLE_REF.partition(":")
    import importlib

    mod = importlib.import_module(module_name)
    assert getattr(mod, attr) is ats.run_sweep
