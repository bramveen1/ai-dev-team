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
    ACTION_ID_TIMEOUT,
    BLOCK_ID_CRON,
    BLOCK_ID_DESTINATION,
    BLOCK_ID_NAME,
    BLOCK_ID_PROMPT,
    BLOCK_ID_TIMEOUT,
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
    async def test_list_empty(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond, store, resolver)
        respond.assert_awaited_once()
        kwargs = respond.call_args.kwargs
        assert "blocks" in kwargs
        flattened = str(kwargs["blocks"])
        assert "no scheduled tasks" in flattened

    async def test_list_includes_agent_tasks(self, store, resolver, ack, respond, client):
        store.create(_make_task(name="my task"))
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond, store, resolver)
        kwargs = respond.call_args.kwargs
        assert "my task" in str(kwargs["blocks"])

    async def test_list_hides_other_agents_tasks(self, store, resolver, ack, respond, client):
        store.create(_make_task(agent_name="sam", name="sam task"))
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond, store, resolver)
        flattened = str(respond.call_args.kwargs["blocks"])
        assert "sam task" not in flattened

    async def test_default_subcommand_is_list(self, store, resolver, ack, respond, client):
        store.create(_make_task(name="default-list"))
        await handlers.handle_tasks_command(ack, _cmd_body(""), client, respond, store, resolver)
        assert "default-list" in str(respond.call_args.kwargs["blocks"])


@pytest.mark.unit
@pytest.mark.asyncio
class TestCreateSubcommand:
    async def test_create_opens_modal(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("create"), client, respond, store, resolver)
        client.views_open.assert_awaited_once()
        view = client.views_open.call_args.kwargs["view"]
        assert view["callback_id"] == MODAL_CALLBACK_CREATE_TASK
        assert view["private_metadata"] == "lisa"


@pytest.mark.unit
@pytest.mark.asyncio
class TestPauseResumeDelete:
    async def test_pause_sets_enabled_false(self, store, resolver, ack, respond, client):
        task = _make_task(enabled=True)
        store.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"pause {task.task_id}"), client, respond, store, resolver)

        assert store.get(task.task_id).enabled is False

    async def test_resume_sets_enabled_true(self, store, resolver, ack, respond, client):
        task = _make_task(enabled=False)
        store.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"resume {task.task_id}"), client, respond, store, resolver)

        assert store.get(task.task_id).enabled is True

    async def test_delete_removes_task(self, store, resolver, ack, respond, client):
        task = _make_task()
        store.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"delete {task.task_id}"), client, respond, store, resolver)

        assert store.get(task.task_id) is None

    async def test_pause_refuses_other_agents_task(self, store, resolver, ack, respond, client):
        sam_task = _make_task(agent_name="sam")
        store.create(sam_task)

        # Resolver still reports the caller as lisa
        await handlers.handle_tasks_command(
            ack, _cmd_body(f"pause {sam_task.task_id}"), client, respond, store, resolver
        )

        # Task remains enabled — scoping prevented the mutation
        assert store.get(sam_task.task_id).enabled is True
        respond.assert_awaited()
        message = respond.call_args.kwargs.get("text", "")
        assert "cannot modify" in message.lower() or "another agent" in message.lower()

    async def test_delete_refuses_other_agents_task(self, store, resolver, ack, respond, client):
        sam_task = _make_task(agent_name="sam")
        store.create(sam_task)

        await handlers.handle_tasks_command(
            ack, _cmd_body(f"delete {sam_task.task_id}"), client, respond, store, resolver
        )

        assert store.get(sam_task.task_id) is not None


@pytest.mark.unit
@pytest.mark.asyncio
class TestDetailSubcommand:
    async def test_detail_returns_full_task(self, store, resolver, ack, respond, client):
        task = _make_task(prompt="Do the detailed thing.")
        store.create(task)

        await handlers.handle_tasks_command(ack, _cmd_body(f"detail {task.task_id}"), client, respond, store, resolver)

        kwargs = respond.call_args.kwargs
        assert "blocks" in kwargs
        rendered = str(kwargs["blocks"])
        assert task.task_id in rendered
        assert "Do the detailed thing." in rendered
        assert task.schedule_cron in rendered

    async def test_detail_missing_id_shows_usage(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("detail"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_detail_unknown_id_shows_not_found(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("detail nonexistent-id"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "not found" in text

    async def test_detail_other_agents_task_shows_scope_error(self, store, resolver, ack, respond, client):
        sam_task = _make_task(agent_name="sam", prompt="Sam's secret prompt.")
        store.create(sam_task)

        await handlers.handle_tasks_command(
            ack, _cmd_body(f"detail {sam_task.task_id}"), client, respond, store, resolver
        )

        text = respond.call_args.kwargs.get("text", "")
        assert "another agent" in text.lower() or "cannot access" in text.lower()
        # Prompt must not be leaked
        assert "Sam's secret prompt." not in str(respond.call_args)


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnknownSubcommand:
    async def test_unknown_shows_help(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("nope"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "Unknown" in text


@pytest.mark.unit
@pytest.mark.asyncio
class TestErrorPaths:
    async def test_resolver_returns_none(self, store, resolver, ack, respond, client):
        resolver.return_value = None
        await handlers.handle_tasks_command(ack, _cmd_body("list"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "Could not determine" in text

    async def test_pause_without_id_shows_usage(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("pause"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_resume_without_id_shows_usage(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("resume"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_delete_without_id_shows_usage(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("delete"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_pause_missing_task_returns_not_found(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("pause missing-id"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "not found" in text

    async def test_delete_missing_task_returns_not_found(self, store, resolver, ack, respond, client):
        await handlers.handle_tasks_command(ack, _cmd_body("delete missing-id"), client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "not found" in text

    async def test_create_without_trigger_id(self, store, resolver, ack, respond, client):
        body = {"text": "create", "trigger_id": "", "user_id": "U"}
        await handlers.handle_tasks_command(ack, body, client, respond, store, resolver)
        text = respond.call_args.kwargs.get("text", "")
        assert "trigger_id" in text


@pytest.mark.unit
@pytest.mark.asyncio
class TestCreateModalSubmission:
    def _view(
        self,
        name="Review",
        prompt="Do the thing",
        cron_expr="0 9 * * 1-5",
        destination="C_DEST",
        agent="lisa",
        timeout="",
    ):
        # The destination element is a conversations_select, so Slack delivers
        # the picked channel under `selected_conversation` (or omits the key
        # entirely if the user submitted nothing).
        dest_payload = {"selected_conversation": destination} if destination else {}
        values = {
            BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": name}},
            BLOCK_ID_PROMPT: {ACTION_ID_PROMPT: {"value": prompt}},
            BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": cron_expr}},
            BLOCK_ID_DESTINATION: {ACTION_ID_DESTINATION: dest_payload},
            BLOCK_ID_TIMEOUT: {ACTION_ID_TIMEOUT: {"value": timeout}},
        }
        return {
            "private_metadata": agent,
            "state": {"values": values},
        }

    async def test_valid_submission_creates_task(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(destination="C_DEST"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        ack.assert_awaited()
        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].destination == "C_DEST"
        assert tasks[0].enabled is True
        # Confirmation is shown in-modal via response_action=update — no DM
        # is sent, so the bot's Messages tab can stay disabled.
        client.chat_postMessage.assert_not_awaited()
        ack_kwargs = ack.call_args.kwargs
        assert ack_kwargs.get("response_action") == "update"
        assert tasks[0].task_id in str(ack_kwargs["view"])

    async def test_invalid_cron_returns_errors(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(cron_expr="bad cron"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        ack.assert_awaited_once()
        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_CRON in kwargs["errors"]
        # No task created on validation failure
        assert store.list_for_agent("lisa") == []

    async def test_missing_name_returns_errors(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(name=""), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_NAME in kwargs["errors"]

    async def test_missing_destination_returns_errors(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(destination=""), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_DESTINATION in kwargs["errors"]
        assert store.list_for_agent("lisa") == []

    async def test_blank_timeout_creates_task_with_none(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(timeout=""), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].timeout_seconds is None

    async def test_valid_timeout_passes_through(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(timeout="1800"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].timeout_seconds == 1800

    async def test_out_of_range_timeout_returns_errors(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(timeout="30"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_TIMEOUT in kwargs["errors"]
        assert store.list_for_agent("lisa") == []

    async def test_non_integer_timeout_returns_errors(self, store, client):
        ack = AsyncMock()
        body = {"view": self._view(timeout="abc"), "user": {"id": "U_USER"}}

        await handlers.handle_create_modal_submission(ack, body, client, store)

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert BLOCK_ID_TIMEOUT in kwargs["errors"]
        assert store.list_for_agent("lisa") == []


@pytest.mark.unit
@pytest.mark.asyncio
class TestRegisterHandlersMultiAgent:
    """Each call to ``register_handlers`` must bind store + resolver to its own
    callbacks. A previous bug stored these in module-level globals, so the last
    registration overwrote earlier ones — every agent's slash command resolved
    to whichever agent was registered last.
    """

    async def test_each_agent_gets_its_own_resolver(self, tmp_path, ack, respond, client):
        lisa_store = ScheduledTaskStore(str(tmp_path / "lisa.db"))
        sam_store = ScheduledTaskStore(str(tmp_path / "sam.db"))
        try:
            lisa_resolver = MagicMock(return_value="lisa")
            sam_resolver = MagicMock(return_value="sam")

            # Capture the @bolt_app.command-decorated callbacks. Stub Bolt with
            # MagicMocks whose decorators simply record the inner function.
            captured: dict[str, dict] = {"lisa": {}, "sam": {}}

            def _make_bolt_stub(agent: str):
                bolt = MagicMock()

                def _command(name):
                    def _decorator(fn):
                        captured[agent]["command_name"] = name
                        captured[agent]["command_fn"] = fn
                        return fn

                    return _decorator

                def _view(callback_id):
                    def _decorator(fn):
                        captured[agent]["view_fn"] = fn
                        return fn

                    return _decorator

                bolt.command = _command
                bolt.view = _view
                return bolt

            handlers.register_handlers(
                _make_bolt_stub("lisa"),
                lisa_store,
                lisa_resolver,
                command_name="/lisa-tasks",
            )
            handlers.register_handlers(
                _make_bolt_stub("sam"),
                sam_store,
                sam_resolver,
                command_name="/sam-tasks",
            )

            # Seed each store so we can tell which one was queried.
            lisa_store.create(_make_task(agent_name="lisa", name="lisa-task"))
            sam_store.create(_make_task(agent_name="sam", name="sam-task"))

            # Invoke Lisa's command callback — it must resolve to lisa, not sam.
            lisa_respond = AsyncMock()
            await captured["lisa"]["command_fn"](AsyncMock(), _cmd_body("list"), client, lisa_respond)
            lisa_resolver.assert_called_once()
            sam_resolver.assert_not_called()
            assert "lisa-task" in str(lisa_respond.call_args.kwargs["blocks"])
            assert "sam-task" not in str(lisa_respond.call_args.kwargs["blocks"])

            # And Sam's command callback resolves to sam.
            sam_respond = AsyncMock()
            await captured["sam"]["command_fn"](AsyncMock(), _cmd_body("list"), client, sam_respond)
            sam_resolver.assert_called_once()
            assert "sam-task" in str(sam_respond.call_args.kwargs["blocks"])
            assert "lisa-task" not in str(sam_respond.call_args.kwargs["blocks"])
        finally:
            lisa_store.close()
            sam_store.close()
