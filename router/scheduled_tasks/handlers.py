"""Slack slash command + modal handlers for scheduled tasks.

Wires up the ``/tasks`` command to the scheduled task store:

    /tasks list
    /tasks create
    /tasks pause <task_id>
    /tasks resume <task_id>
    /tasks delete <task_id>

Ownership is scoped to the calling agent — the agent that owns the bot that
received the command. The agent resolver is injected so it can share the
router's existing bot-user-ID → agent mapping.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from router.scheduled_tasks import cron
from router.scheduled_tasks.block_kit import (
    MODAL_CALLBACK_CREATE_TASK,
    build_create_task_modal,
    build_task_list_message,
    parse_create_modal_submission,
)
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore, ScopeError

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

# Agent resolver: given the slash command payload, return the agent name that
# owns this invocation. Keeps Sam from editing Lisa's tasks by construction.
AgentResolver = Callable[[dict[str, Any]], str | None]

_store: ScheduledTaskStore | None = None
_resolve_agent: AgentResolver | None = None


def _get_store() -> ScheduledTaskStore:
    if _store is None:
        raise RuntimeError("Scheduled tasks handlers not registered — call register_handlers() first")
    return _store


def _get_resolver() -> AgentResolver:
    if _resolve_agent is None:
        raise RuntimeError("Scheduled tasks handlers not registered — call register_handlers() first")
    return _resolve_agent


def _parse_command(text: str) -> tuple[str, list[str]]:
    """Split the slash command text into ``(subcommand, args)``."""
    parts = (text or "").strip().split()
    if not parts:
        return "list", []
    return parts[0].lower(), parts[1:]


async def handle_tasks_command(ack: Any, body: dict[str, Any], client: Any, respond: Any) -> None:
    """Top-level handler for ``/tasks``. Dispatches to the appropriate subcommand."""
    await ack()

    resolver = _get_resolver()
    agent_name = resolver(body)
    if agent_name is None:
        await respond(
            text="Could not determine which agent owns this command. Try again from the agent's channel or DM."
        )
        return

    subcommand, args = _parse_command(body.get("text", ""))

    if subcommand == "list":
        await _handle_list(agent_name, respond)
    elif subcommand == "create":
        await _handle_create_open(agent_name, body, client, respond)
    elif subcommand == "pause":
        await _handle_pause(agent_name, args, respond, enabled=False)
    elif subcommand == "resume":
        await _handle_pause(agent_name, args, respond, enabled=True)
    elif subcommand == "delete":
        await _handle_delete(agent_name, args, respond)
    else:
        await respond(text=f"Unknown subcommand `{subcommand}`. Try: list, create, pause, resume, delete.")


async def _handle_list(agent_name: str, respond: Any) -> None:
    store = _get_store()
    tasks = store.list_for_agent(agent_name)
    message = build_task_list_message(agent_name, tasks)
    await respond(blocks=message["blocks"], text=f"{agent_name.capitalize()}'s scheduled tasks")


async def _handle_create_open(agent_name: str, body: dict[str, Any], client: Any, respond: Any) -> None:
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        await respond(text="Could not open the create task modal — missing trigger_id.")
        return

    try:
        await client.views_open(trigger_id=trigger_id, view=build_create_task_modal(agent_name))
    except Exception:
        logger.exception("Failed to open create task modal for agent=%s", agent_name)
        await respond(text="Sorry, I couldn't open the task creation modal.")


async def _handle_pause(agent_name: str, args: list[str], respond: Any, enabled: bool) -> None:
    if not args:
        verb = "resume" if enabled else "pause"
        await respond(text=f"Usage: `/tasks {verb} <task_id>`")
        return

    task_id = args[0]
    store = _get_store()
    try:
        task = store.set_enabled(task_id, enabled=enabled, agent_name=agent_name)
    except ScopeError:
        await respond(text=f"You cannot modify task `{task_id}` — it belongs to another agent.")
        return
    except KeyError:
        await respond(text=f"Task `{task_id}` not found.")
        return

    state = "resumed" if enabled else "paused"
    await respond(text=f"Task *{task.name}* ({task_id}) {state}.")


async def _handle_delete(agent_name: str, args: list[str], respond: Any) -> None:
    if not args:
        await respond(text="Usage: `/tasks delete <task_id>`")
        return

    task_id = args[0]
    store = _get_store()
    try:
        deleted = store.delete(task_id, agent_name=agent_name)
    except ScopeError:
        await respond(text=f"You cannot delete task `{task_id}` — it belongs to another agent.")
        return

    if deleted:
        await respond(text=f"Task `{task_id}` deleted.")
    else:
        await respond(text=f"Task `{task_id}` not found.")


async def handle_create_modal_submission(ack: Any, body: dict[str, Any], client: Any) -> None:
    """Handle ``view_submission`` for the create task modal."""
    view = body.get("view", {})
    values = parse_create_modal_submission(view)
    errors: dict[str, str] = {}

    if not values["name"]:
        errors["task_name"] = "Name is required."
    if not values["prompt"]:
        errors["task_prompt"] = "Prompt is required."
    if not values["schedule_cron"]:
        errors["task_cron"] = "Schedule is required."
    else:
        try:
            cron.validate(values["schedule_cron"])
        except cron.CronError as e:
            errors["task_cron"] = str(e)

    if errors:
        await ack(response_action="errors", errors=errors)
        return

    await ack()

    now = datetime.now(timezone.utc)
    next_run = cron.next_run_after(values["schedule_cron"], now)
    task = ScheduledTask(
        task_id=str(uuid.uuid4()),
        agent_name=values["agent_name"],
        name=values["name"],
        prompt=values["prompt"],
        schedule_cron=values["schedule_cron"],
        destination=values["destination"],
        enabled=True,
        created_at=now,
        next_run_at=next_run,
    )

    store = _get_store()
    store.create(task)

    user_id = body.get("user", {}).get("id")
    if user_id:
        try:
            await client.chat_postMessage(
                channel=user_id,
                text=(
                    f"Created scheduled task *{task.name}* for {task.agent_name.capitalize()}.\n"
                    f"task_id: `{task.task_id}` · next run: {task.next_run_at.isoformat()}"
                ),
            )
        except Exception:
            logger.exception("Failed to confirm scheduled task creation to user=%s", user_id)


def register_handlers(
    bolt_app: AsyncApp,
    store: ScheduledTaskStore,
    agent_resolver: AgentResolver,
) -> None:
    """Register ``/tasks`` and the create-modal submission handler with ``bolt_app``."""
    global _store, _resolve_agent
    _store = store
    _resolve_agent = agent_resolver

    @bolt_app.command("/tasks")
    async def tasks_command(ack, body, client, respond):
        await handle_tasks_command(ack, body, client, respond)

    @bolt_app.view(MODAL_CALLBACK_CREATE_TASK)
    async def create_modal(ack, body, client):
        await handle_create_modal_submission(ack, body, client)
