"""Tests for the /tasks slash command handlers and scoping."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.chat.adapters.slack_forms import INPUT_REQUEST_CALLBACK_ID, register_input_request_handlers
from router.scheduled_tasks import cron, handlers
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
    return {"text": text, "trigger_id": trigger_id, "user_id": "U_USER", "channel_id": "C_CMD", "message_ts": "1.0"}


def _capture_input_handlers() -> dict:
    """Register the shared InputRequest modal listeners on a stub Bolt app."""
    captured: dict[str, object] = {}
    bolt = MagicMock()

    def _view(callback_id):
        def _decorator(fn):
            captured["submission"] = fn
            return fn

        return _decorator

    def _view_closed(callback_id):
        def _decorator(fn):
            captured["closed"] = fn
            return fn

        return _decorator

    bolt.view = _view
    bolt.view_closed = _view_closed
    register_input_request_handlers(bolt)
    return captured


def _state_values(name="Review", prompt="Do the thing", cron_expr="0 9 * * 1-5", destination="C_DEST", timeout=""):
    """A view_submission state payload for the create-task InputRequest form.

    The destination element is a conversations_select, so Slack delivers the
    picked channel under ``selected_conversation`` (or omits the key entirely
    when nothing was submitted).
    """
    return {
        "name": {"value": {"value": name}},
        "prompt": {"value": {"value": prompt}},
        "schedule_cron": {"value": {"value": cron_expr}},
        "destination": {"value": {"selected_conversation": destination} if destination else {}},
        "timeout_seconds": {"value": {"value": timeout}},
    }


async def _start_create(store, resolver, client, respond) -> tuple[asyncio.Task, str]:
    """Kick off `/tasks create` and wait until the modal has been opened.

    Returns the in-flight handler task and the pending form's request_id
    (the view's ``private_metadata``).
    """
    task = asyncio.create_task(
        handlers.handle_tasks_command(AsyncMock(), _cmd_body("create"), client, respond, store, resolver)
    )
    for _ in range(200):
        if client.views_open.await_count:
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise AssertionError("views_open was never called")
    view = client.views_open.call_args.kwargs["view"]
    return task, view["private_metadata"]


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
    async def test_create_opens_input_request_modal(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        view = client.views_open.call_args.kwargs["view"]
        assert view["callback_id"] == INPUT_REQUEST_CALLBACK_ID
        assert "Lisa" in view["title"]["text"]
        assert [b["block_id"] for b in view["blocks"]] == [
            "name",
            "prompt",
            "schedule_cron",
            "destination",
            "timeout_seconds",
        ]

        # Close the modal to end the flow; cancelling stays silent (parity
        # with the legacy modal, where closing did nothing).
        await input_handlers["closed"](AsyncMock(), {"view": {"private_metadata": request_id}})
        await asyncio.wait_for(task, timeout=5)
        respond.assert_not_awaited()


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
class TestCreateModalFlow:
    """The create-task form driven end-to-end through the generic InputRequest modal.

    Parity contract (#747): field validation errors keep the modal open via
    ``response_action="errors"`` exactly like the legacy bespoke modal did;
    a fully-valid submission closes the modal and creates the task.
    """

    async def _submit(self, input_handlers, request_id: str, values: dict) -> AsyncMock:
        ack = AsyncMock()
        await input_handlers["submission"](ack, {"view": {"private_metadata": request_id, "state": {"values": values}}})
        return ack

    async def _finish(self, input_handlers, request_id: str, task: asyncio.Task) -> None:
        await input_handlers["closed"](AsyncMock(), {"view": {"private_metadata": request_id}})
        await asyncio.wait_for(task, timeout=5)

    async def test_valid_submission_creates_task(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(destination="C_DEST"))
        await asyncio.wait_for(task, timeout=5)

        # Plain ack — Slack closes the modal on a valid submission.
        ack.assert_awaited_once_with()
        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].destination == "C_DEST"
        assert tasks[0].enabled is True
        # Confirmation goes through the slash command's respond (response_url),
        # so the bot's Messages tab can stay disabled.
        client.chat_postMessage.assert_not_awaited()
        respond.assert_awaited_once()
        confirmation = respond.call_args.kwargs["text"]
        assert tasks[0].task_id in confirmation
        assert "Created" in confirmation

    async def test_invalid_cron_returns_errors(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(cron_expr="bad cron"))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "schedule_cron" in kwargs["errors"]
        # No task created on validation failure; the modal stays open.
        assert store.list_for_agent("lisa") == []
        assert not task.done()
        await self._finish(input_handlers, request_id, task)

    async def test_missing_name_returns_errors(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(name=""))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "name" in kwargs["errors"]
        await self._finish(input_handlers, request_id, task)

    async def test_missing_destination_returns_errors(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(destination=""))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "destination" in kwargs["errors"]
        assert store.list_for_agent("lisa") == []
        await self._finish(input_handlers, request_id, task)

    async def test_blank_timeout_creates_task_with_none(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        await self._submit(input_handlers, request_id, _state_values(timeout=""))
        await asyncio.wait_for(task, timeout=5)

        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].timeout_seconds is None

    async def test_valid_timeout_passes_through(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        await self._submit(input_handlers, request_id, _state_values(timeout="1800"))
        await asyncio.wait_for(task, timeout=5)

        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].timeout_seconds == 1800

    async def test_out_of_range_timeout_returns_errors(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(timeout="30"))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "timeout_seconds" in kwargs["errors"]
        assert store.list_for_agent("lisa") == []
        await self._finish(input_handlers, request_id, task)

    async def test_non_integer_timeout_returns_errors(self, store, resolver, respond, client):
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(timeout="abc"))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "timeout_seconds" in kwargs["errors"]
        assert store.list_for_agent("lisa") == []
        await self._finish(input_handlers, request_id, task)

    async def test_impossible_cron_returns_field_error_no_task_created(self, store, resolver, respond, client):
        # "0 0 30 2 *" is syntactically valid but Feb never has a 30th day.
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(cron_expr="0 0 30 2 *"))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "schedule_cron" in kwargs["errors"]
        assert store.list_for_agent("lisa") == []
        await self._finish(input_handlers, request_id, task)

    async def test_apr31_cron_returns_field_error_no_task_created(self, store, resolver, respond, client):
        # Apr never has a 31st day — same satisfiability check via validate().
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(cron_expr="0 0 31 4 *"))

        kwargs = ack.call_args.kwargs
        assert kwargs.get("response_action") == "errors"
        assert "schedule_cron" in kwargs["errors"]
        assert store.list_for_agent("lisa") == []
        await self._finish(input_handlers, request_id, task)

    async def test_feb29_cron_creates_task_on_next_leap_year(self, store, resolver, respond, client):
        # "0 0 29 2 *" is a valid leap-day schedule — no error, task created
        # with next_run_at on the next Feb 29.
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        ack = await self._submit(input_handlers, request_id, _state_values(cron_expr="0 0 29 2 *"))
        await asyncio.wait_for(task, timeout=5)

        ack.assert_awaited_once_with()
        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].schedule_cron == "0 0 29 2 *"
        assert tasks[0].next_run_at.month == 2
        assert tasks[0].next_run_at.day == 29

    async def test_next_run_after_cron_error_surfaces_no_task_created(self, store, resolver, respond, client):
        # Even if validate() passes, a CronError from next_run_after must be
        # surfaced to the user rather than propagated as an unhandled exception.
        input_handlers = _capture_input_handlers()
        task, request_id = await _start_create(store, resolver, client, respond)

        with patch("router.scheduled_tasks.input_request.cron.next_run_after", side_effect=cron.CronError("forced")):
            await self._submit(input_handlers, request_id, _state_values())
            await asyncio.wait_for(task, timeout=5)

        assert store.list_for_agent("lisa") == []
        respond.assert_awaited_once()
        assert "Could not schedule" in respond.call_args.kwargs["text"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestCreateScriptedTransportParity:
    """A no-modal transport fulfils `tasks create` via scripted Q&A (#747)."""

    async def test_execute_tasks_create_over_scripted_adapter(self, store):
        from router.chat.interface import ChatAdapter
        from router.chat.types import (
            AdapterCapabilities,
            ConversationRef,
            InboundMessage,
            OutboundMessage,
            PrincipalRef,
            StructuredResponse,
        )
        from router.commands.types import Command

        replies = ["nightly-backup", "Summarize the inbox.", "0 2 * * *", "tui-session", "900"]

        class FakeAdapter(ChatAdapter):
            """In-memory TUI-style stub (same shape as the #122 parity fixture)."""

            def __init__(self):
                self.history: list = []
                self.sent: list[str] = []
                self._pending = list(replies)

            @property
            def capabilities(self):
                return AdapterCapabilities()  # supports_forms=False → scripted

            async def send_message(self, outbound):
                self.sent.append(outbound.text)
                self.history.append(outbound)

            async def read_thread(self, conversation_ref):
                if self._pending and self.history and isinstance(self.history[-1], OutboundMessage):
                    self.history.append(
                        InboundMessage(
                            conversation_ref=ConversationRef("tui:1"),
                            principal_ref=PrincipalRef("local:user"),
                            text=self._pending.pop(0),
                        )
                    )
                return list(self.history)

            async def set_status(self, conversation_ref, state):
                pass

            def resolve_principal(self, raw_user_id):
                return PrincipalRef(raw_user_id)

            def parse_mentions(self, text, conversation_ref):
                return []

            async def prompt_for_choice(self, conversation_ref, prompt):
                return StructuredResponse(choice=prompt.choices[0], index=0)

            async def collect_input(self, conversation_ref, request):
                raise AssertionError("supports_forms=False — core must use the scripted fallback")

        adapter = FakeAdapter()
        cmd = Command(verb="tasks", args=["create"], transport="tui", conversation_ref="tui:1")

        result = await handlers.execute_tasks_command(
            cmd,
            subject_agent="lisa",
            store=store,
            adapter=adapter,
            conversation_ref="tui:1",
        )

        assert result.ok is True
        tasks = store.list_for_agent("lisa")
        assert len(tasks) == 1
        assert tasks[0].name == "nightly-backup"
        assert tasks[0].schedule_cron == "0 2 * * *"
        assert tasks[0].destination == "tui-session"
        assert tasks[0].timeout_seconds == 900
        # The scripted collector asked one question per field, title first.
        assert "New task for Lisa" in adapter.sent[0]

    async def test_execute_tasks_create_without_adapter_errors(self, store):
        from router.commands.types import Command

        cmd = Command(verb="tasks", args=["create"], transport="tui")
        result = await handlers.execute_tasks_command(cmd, subject_agent="lisa", store=store)
        assert result.ok is False
        assert "interactive" in result.text


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
