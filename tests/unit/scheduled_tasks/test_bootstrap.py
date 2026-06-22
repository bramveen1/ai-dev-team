"""Tests for scheduled tasks bootstrap wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.scheduled_tasks.bootstrap import (
    open_store,
    setup_scheduled_tasks_handlers,
    start_scheduled_tasks_scheduler,
)
from router.scheduled_tasks.seeds import SeedTask

# Fixed fixture tasks independent of config/agents/ (which is absent on bare checkouts).
_FIXTURE_TASKS: tuple[SeedTask, ...] = (
    SeedTask(
        agent_name="lisa",
        name="Daily inbox review",
        prompt="Summarize inbox activity.",
        schedule_cron="0 9 * * 1-5",
        enabled=False,
    ),
)


@pytest.mark.unit
def test_open_store_seeds_defaults(tmp_path, monkeypatch):
    # Patch DEFAULT_SEED_TASKS so the test runs on bare checkouts where
    # config/agents/ does not exist and the real DEFAULT_SEED_TASKS is empty.
    import router.scheduled_tasks.seeds as seeds_mod

    monkeypatch.setattr(seeds_mod, "DEFAULT_SEED_TASKS", _FIXTURE_TASKS)

    db_path = str(tmp_path / "bootstrap.db")
    store = open_store(db_path=db_path)
    try:
        assert any(t.name == "Daily inbox review" for t in store.list_for_agent("lisa"))
    finally:
        store.close()


@pytest.mark.unit
def test_open_store_can_skip_seeds(tmp_path):
    db_path = str(tmp_path / "bootstrap-no-seed.db")
    store = open_store(db_path=db_path, seed_defaults=False)
    try:
        assert store.list_for_agent("lisa") == []
    finally:
        store.close()


@pytest.mark.unit
def test_setup_handlers_uses_default_command_name(tmp_path):
    bolt_app = MagicMock()
    bolt_app.command = MagicMock(return_value=lambda fn: fn)
    bolt_app.view = MagicMock(return_value=lambda fn: fn)

    store = open_store(db_path=str(tmp_path / "h.db"), seed_defaults=False)
    try:
        setup_scheduled_tasks_handlers(
            bolt_app=bolt_app,
            store=store,
            agent_resolver=lambda body: "lisa",
        )
        bolt_app.command.assert_called_with("/tasks")
    finally:
        store.close()


@pytest.mark.unit
def test_setup_handlers_accepts_per_agent_command_list(tmp_path):
    """Multi-agent setup registers every agent's command on every Bolt app."""
    bolt_app = MagicMock()
    bolt_app.command = MagicMock(return_value=lambda fn: fn)
    bolt_app.view = MagicMock(return_value=lambda fn: fn)

    store = open_store(db_path=str(tmp_path / "h-list.db"), seed_defaults=False)
    try:
        setup_scheduled_tasks_handlers(
            bolt_app=bolt_app,
            store=store,
            agent_resolver=lambda body: None,
            command_name=["/lisa-tasks", "/sam-tasks"],
        )
        registered = [c.args[0] for c in bolt_app.command.call_args_list]
        assert "/lisa-tasks" in registered
        assert "/sam-tasks" in registered
    finally:
        store.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_scheduler_returns_running_task(tmp_path):
    store = open_store(db_path=str(tmp_path / "s.db"), seed_defaults=False)
    try:
        task = start_scheduled_tasks_scheduler(
            store=store,
            client_resolver=lambda _agent: MagicMock(),
            dispatch_fn=AsyncMock(return_value={"response": "ok"}),
        )
        assert isinstance(task, asyncio.Task)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
        store.close()
