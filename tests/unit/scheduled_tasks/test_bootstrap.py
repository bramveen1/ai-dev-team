"""Tests for scheduled tasks bootstrap wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.scheduled_tasks.bootstrap import setup_scheduled_tasks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bootstrap_registers_handlers_and_starts_scheduler(tmp_path):
    bolt_app = MagicMock()
    bolt_app.command = MagicMock(return_value=lambda fn: fn)
    bolt_app.view = MagicMock(return_value=lambda fn: fn)

    slack_client = MagicMock()
    dispatch_fn = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "ok"})

    db_path = str(tmp_path / "bootstrap.db")
    store, scheduler_task = setup_scheduled_tasks(
        bolt_app=bolt_app,
        slack_client=slack_client,
        dispatch_fn=dispatch_fn,
        agent_resolver=lambda body: "lisa",
        db_path=db_path,
    )

    try:
        # Seeds should be present in the store
        assert any(t.name == "Daily inbox review" for t in store.list_for_agent("lisa"))
        # Slash command handler registered
        bolt_app.command.assert_called_with("/tasks")
        # Scheduler coroutine running
        assert not scheduler_task.done()
    finally:
        scheduler_task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await scheduler_task
        store.close()
