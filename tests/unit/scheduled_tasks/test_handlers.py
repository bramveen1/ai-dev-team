"""Tests for the /tasks slash command handlers and scoping."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.scheduled_tasks import handlers
from router.scheduled_tasks.block_kit import (
    ACTION_ID_CRON,
    ACTION_ID_DESTINATION,
    ACTION_ID_NAME,
    ACTION_ID_PROMPT,
    BLOCK_ID_CRON,
    BLOCK_ID_DESTINATION,
    BLOCK_ID_NAME,
    BLOCK_ID_PROMPT,
    MODAL_CALLBACK_CREATE_TASK,
)
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore


def _make_task(**overrides) -> ScheduledTask:
    defaults = {
        "task_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "name": "Daily inbox review",
        "prompt": "Summarize yesterday's inbox.",
        "schedule_cron": "0 9 * * 1-5",
        "next_run_at": datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc),
        "destination": None,
        "enabled": True,
        "created_at": datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.fixture
def resolver():
    return MagicMock(return_value="lisa")


@pytest.fixture
def wired(store, resolver):
    handlers._store = store
    handlers._resolve_agent = resolver
    yield store
    handlers._store = None
    handlers._resolve_agent = None


@pytest.fixture
def ack():
    return AsyncMock()


@pytest.fixture
def respond():
    return AsyncMock()


@pytest.fixture
def client():
    c = MagicMock()
    c.views_open = AsyncMock(return_value={"ok": True})
    c.chat_postMessage = AsyncMock(return_value={"ok": True})
    return c


def _cmd_body(text: str, trigger_id: str = "trigger-123") -> dict:
    return {"text": text, "trigger_id": trigger_id, "user_id": "U_USER"}


@pytest.mark.unit
@pytest.mark.asyncio
class TestListSubcommand:
    async def test_list_empty(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond)
        respond.assert_awaited_once()
        kwargs = respond.call_args.kwargs
        assert "blocks" in kwargs
        flattened = str(kwargs["blocks"])
        assert "no scheduled tasks" in flattened

    async def test_list_includes_agent_tasks(self, wired, ack, respond, client):
        wired.create(_make_task(name="my task"))
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond)
        kwargs = respond.call_args.kwargs
        assert "my task" in str(kwargs["blocks"])

    async def test_list_hides_other_agents_tasks(self, wired, ack, respond, client):
        wired.create(_make_task(agent_name="sam", name="sam task"))
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond)
        flattened = str(respond.call_args.kwargs["blocks"])
        assert "sam task" not in flattened

    async def test_default_subcommand_is_list(self, wired, ack, respond, client):
        wired.create(_make_task(name="default-list"))
        await handlers.handle_tasks_command(ack, _cmd_body(""), client, respond)
        assert "default-list" in str(respond.call_args.kwargs["blocks"])


@pytest.mark.unit
@pytest.mark.asyncio
class TestCreateSubcommand:
    async def test_create_opens_modal(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("create"), client, respond)
        client.views_open.assert_awaited_once()
        view = client.views_open.call_args.kwargs["view"]
        assert view["callback_id"] == MODAL_CALLBACK_CREATE_TASK
        assert view["private_metadata"] == "lisa"


@pytest.mark.unit
@pytest.mark.asyncio
class TestPauseResumeDelete:
    async def test_pause_sets_enabled_false(self, wired, ack, respond, client):
        task = _make_task(enabled=True)
        wired.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"pause {task.task_id}"), client, respond)

        assert wired.get(task.task_id).enabled is False

    async def test_resume_sets_enabled_true(self, wired, ack, respond, client):
        task = _make_task(enabled=False)
        wired.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"resume {task.task_id}"), client, respond)

        assert wired.get(task.task_id).enabled is True

    async def test_delete_removes_task(self, wired, ack, respond, client):
        task = _make_task()
        wired.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"delete {task.task_id}"), client, respond)

        assert wired.get(task.task_id) is None

    async def test_pause_refuses_other_agents_task(self, wired, ack, respond, client):
        sam_task = _make_task(agent_name="sam")
        wired.create(sam_task)

        # Resolver still reports the caller as lisa
        await handlers.handle_tasks_command(ack, _cmd_body(f"pause {sam_task.task_id}"), client, respond)

        # Task remains enabled — scoping prevented the mutation
        assert wired.get(sam_task.task_id).enabled is True
        respond.assert_awaited()
        message = respond.call_args.kwargs.get("text", "")
        assert "cannot modify" in message.lower() or "another agent" in message.lower()

    async def test_delete_refuses_other_agents_task(self, wired, ack, respond, client):
        sam_task = _make_task(agent_name="sam")
        wired.create(sam_task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"delete {sam_task.task_id}"), client, respond)

        assert wired.get(sam_task.task_id) is not None


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnknownSubcommand:
    async def test_unknown_shows_help(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("nope"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "Unknown" in text


@pytest.mark.unit
@pytest.mark.asyncio
class TestErrorPaths:
    async def test_resolver_returns_none(self, wired, ack, respond, client, resolver):
        resolver.return_value = None
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "Could not determine" in text

    async def test_pause_without_id_shows_usage(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("pause"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_resume_without_id_shows_usage(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("resume"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_delete_without_id_shows_usage(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("delete"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_pause_missing_task_returns_not_found(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("pause missing-id"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "not found" in text

    async def test_delete_missing_task_returns_not_found(self, wired, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("delete missing-id"), client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "not found" in text

    async def test_create_without_trigger_id(self, wired, ack, respond, client):
        body = {"text": "create", "trigger_id": "", "user_id": "U"}
        await handlers.handle_tasks_command(ack, body, client, respond)
        text = respond.call_args.kwargs.get("text", "")
        assert "trigger_id" in text

    async def test_get_store_requires_registration(self):
        handlers._store = None
        handlers._resolve_agent = None
        with pytest.raises(RuntimeError):
            handlers._get_store()
        with pytest.raises(RuntimeError):
            handlers._get_resolver()


@pytest.mark.unit
@pytest.mark.asyncio
class TestCreateModalSubmission:
    def _view(self, name="Review", prompt="Do the thing", cron_expr="0 9 * * 1-5", destination="", agent="lisa"):
        return {
            "private_metadata": agent,
            "state": {
                "values": {
                    BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": name}},
                    BLOCK_ID_PROMPT: {ACTION_ID_PROMPT: {"value": prompt}},
                    BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": cron_expr}},
                    BLOCK_ID_DESTINATION: {ACTION_ID_DESTINATION: {"value": destination}},
                }
            },
        }

    async def test_valid_submission_creates_task(self, wired, client):
        ack = AsyncMock()
        body = {"view": self._view(destination="C_DEST"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client)

        ack.assert_awaited()
        tasks = wired.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].destination == "C_DEST"
        assert tasks[0].enabled is True
        client.chat_postMessage.assert_awaited_once()

    async def test_invalid_cron_returns_errors(self, wired, client):
        ack = AsyncMock()
        body = {"view": self._view(cron_expr="bad cron"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client)

        ack.assert_awaited_once()
        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_CRON in kwargs["errors"]
        # No task created on validation failure
        assert wired.list_for_agent("lisa") == []

    async def test_missing_name_returns_errors(self, wired, client):
        ack = AsyncMock()
        body = {"view": self._view(name=""), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client)

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_NAME in kwargs["errors"]
