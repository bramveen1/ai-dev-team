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
    BLOCK_ID_TIMEOUT,
    MODAL_CALLBACK_CREATE_TASK,
    TIMEOUT_MAX,
    TIMEOUT_MIN,
    build_create_task_confirmation_view,
    build_create_task_modal,
    build_task_detail_message,
    build_task_list_message,
    parse_create_modal_submission,
)
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore, ScopeError

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from router.commands.types import Command

logger = logging.getLogger(__name__)

# Agent resolver: given the slash command payload, return the agent name that
# owns this invocation. Keeps Sam from editing Lisa's tasks by construction.
AgentResolver = Callable[[dict[str, Any]], str | None]


def _parse_command(text: str) -> tuple[str, list[str]]:
    """Split the slash command text into ``(subcommand, args)``."""
    parts = (text or "").strip().split()
    if not parts:
        return "list", []
    return parts[0].lower(), parts[1:]


async def handle_tasks_command(
    ack: Any,
    body: dict[str, Any],
    client: Any,
    respond: Any,
    store: ScheduledTaskStore,
    agent_resolver: AgentResolver,
) -> None:
    """Top-level handler for ``/tasks``. Dispatches to the appropriate subcommand.

    ``store`` and ``agent_resolver`` are passed in explicitly so each Bolt app
    keeps its own bindings — module-level globals would be overwritten on each
    re-registration in a multi-agent deployment.
    """
    await ack()

    agent_name = agent_resolver(body)
    if agent_name is None:
        await respond(
            text="Could not determine which agent owns this command. Try again from the agent's channel or DM."
        )
        return

    subcommand, args = _parse_command(body.get("text", ""))

    if subcommand == "list":
        await _handle_list(agent_name, store, respond)
    elif subcommand == "create":
        await _handle_create_open(agent_name, body, client, respond)
    elif subcommand == "pause":
        await _handle_pause(agent_name, args, store, respond, enabled=False)
    elif subcommand == "resume":
        await _handle_pause(agent_name, args, store, respond, enabled=True)
    elif subcommand == "delete":
        await _handle_delete(agent_name, args, store, respond)
    elif subcommand == "detail":
        await _handle_detail(agent_name, args, store, respond)
    else:
        await respond(text=f"Unknown subcommand `{subcommand}`. Try: list, create, pause, resume, delete, detail.")


async def _handle_list(agent_name: str, store: ScheduledTaskStore, respond: Any) -> None:
    tasks = store.list_for_agent(agent_name)
    message = build_task_list_message(agent_name, tasks)
    await respond(blocks=message["blocks"], text=f"{agent_name.capitalize()}'s scheduled tasks")


async def _handle_detail(agent_name: str, args: list[str], store: ScheduledTaskStore, respond: Any) -> None:
    if not args:
        await respond(text="Usage: `/tasks detail <task_id>`")
        return

    task_id = args[0]
    try:
        task = store.get(task_id, agent_name=agent_name)
    except ScopeError:
        await respond(text=f"You cannot access task `{task_id}` — it belongs to another agent.")
        return

    if task is None:
        await respond(text=f"Task `{task_id}` not found.")
        return

    message = build_task_detail_message(task)
    await respond(blocks=message["blocks"], text=f"Task detail: {task.name}")


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


async def _handle_pause(
    agent_name: str,
    args: list[str],
    store: ScheduledTaskStore,
    respond: Any,
    enabled: bool,
) -> None:
    if not args:
        verb = "resume" if enabled else "pause"
        await respond(text=f"Usage: `/tasks {verb} <task_id>`")
        return

    task_id = args[0]
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


async def _handle_delete(agent_name: str, args: list[str], store: ScheduledTaskStore, respond: Any) -> None:
    if not args:
        await respond(text="Usage: `/tasks delete <task_id>`")
        return

    task_id = args[0]
    try:
        deleted = store.delete(task_id, agent_name=agent_name)
    except ScopeError:
        await respond(text=f"You cannot delete task `{task_id}` — it belongs to another agent.")
        return

    if deleted:
        await respond(text=f"Task `{task_id}` deleted.")
    else:
        await respond(text=f"Task `{task_id}` not found.")


async def handle_create_modal_submission(
    ack: Any,
    body: dict[str, Any],
    client: Any,
    store: ScheduledTaskStore,
) -> None:
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
    if not values["destination"]:
        errors["task_destination"] = "Pick a channel or DM where the agent should post."
    if values["timeout_seconds"] is not None and not (TIMEOUT_MIN <= values["timeout_seconds"] <= TIMEOUT_MAX):
        errors[BLOCK_ID_TIMEOUT] = f"Timeout must be a whole number between {TIMEOUT_MIN} and {TIMEOUT_MAX} seconds."

    if errors:
        await ack(response_action="errors", errors=errors)
        return

    now = datetime.now(timezone.utc)
    try:
        next_run = cron.next_run_after(values["schedule_cron"], now)
    except cron.CronError as e:
        await ack(response_action="errors", errors={"task_cron": str(e)})
        return
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
        timeout_seconds=values["timeout_seconds"],
    )

    store.create(task)

    # Confirm by swapping the modal view rather than posting a DM. Posting to
    # the user only works if the bot's Messages tab is enabled, which isn't a
    # given for every per-agent app. ``response_action: "update"`` is purely a
    # client-side instruction to Slack — no extra API calls, no permissions.
    await ack(
        response_action="update",
        view=build_create_task_confirmation_view(task),
    )


async def handle_tasks_command_from_parsed(
    cmd: "Command",
    *,
    ack: Any,
    body: dict[str, Any],
    client: Any,
    respond: Any,
    store: ScheduledTaskStore,
    agent_resolver: AgentResolver,
) -> None:
    """Process a ``tasks`` :class:`~router.commands.Command` from the grammar.

    Called by the Slack forwarding shim in :func:`register_handlers`.
    ``cmd.args`` provides the subcommand and any further arguments, mirroring
    what :func:`_parse_command` produced from the raw slash body text.

    ``tasks create`` dispatches to the existing Slack modal-open path
    unchanged (the #551↔#552 boundary — structured input is #552).
    """
    await ack()

    agent_name = agent_resolver(body)
    if agent_name is None:
        await respond(
            text="Could not determine which agent owns this command. Try again from the agent's channel or DM."
        )
        return

    subcommand = cmd.args[0].lower() if cmd.args else "list"
    args = cmd.args[1:] if len(cmd.args) > 1 else []

    if subcommand == "list":
        await _handle_list(agent_name, store, respond)
    elif subcommand == "create":
        await _handle_create_open(agent_name, body, client, respond)
    elif subcommand == "pause":
        await _handle_pause(agent_name, args, store, respond, enabled=False)
    elif subcommand == "resume":
        await _handle_pause(agent_name, args, store, respond, enabled=True)
    elif subcommand == "delete":
        await _handle_delete(agent_name, args, store, respond)
    elif subcommand == "detail":
        await _handle_detail(agent_name, args, store, respond)
    else:
        await respond(text=f"Unknown subcommand `{subcommand}`. Try: list, create, pause, resume, delete, detail.")


def register_handlers(
    bolt_app: AsyncApp,
    store: ScheduledTaskStore,
    agent_resolver: AgentResolver,
    command_name: str | list[str] = "/tasks",
) -> None:
    """Register the scheduled-tasks slash command + create-modal handler.

    The Bolt callback is a forwarding shim only: it constructs the bare
    ``tasks <subcommand>`` verb text from the slash body, parses it via the
    grammar (:func:`~router.commands.grammar.parse`), and delegates to
    :func:`handle_tasks_command_from_parsed`.

    ``command_name`` is the Slack slash command to register. Pass a list to
    register multiple commands on the same Bolt app — useful when one Slack
    App owns several per-agent commands (e.g. a single dev bot exposing
    ``/dev-lisa-tasks`` *and* ``/dev-sam-tasks``), or when registering every
    agent's command on every Bolt app to tolerate Socket Mode's load-balanced
    delivery between sockets that share a Slack App.

    ``store`` and ``agent_resolver`` are captured in the inner callbacks'
    closure, so each call here registers an *independent* binding. Module-level
    globals would be overwritten on every call, which silently broke ownership
    in multi-agent deployments — every command resolved to whichever agent was
    registered last.
    """
    from router.commands.grammar import parse

    command_names = [command_name] if isinstance(command_name, str) else list(command_name)

    for cmd in command_names:

        @bolt_app.command(cmd)
        async def tasks_command(ack, body, client, respond):
            channel = body.get("channel_id") or ""
            thread_ts = body.get("thread_ts") or body.get("message_ts") or ""
            conversation_ref = f"slack:{channel}:{thread_ts}" if channel else None
            principal_ref = f"slack:{body.get('user_id')}" if body.get("user_id") else None
            body_text = (body.get("text") or "").strip()
            # All variants of this slash command (e.g. /lisa-tasks, /dev-tasks) are
            # task commands — construct the canonical "tasks <sub>" verb text directly.
            verb_text = f"tasks {body_text}".strip()
            parsed_cmd = parse(
                verb_text,
                conversation_ref=conversation_ref,
                principal_ref=principal_ref,
                transport="slack",
            )
            if parsed_cmd is None:
                await ack()
                await respond(text=":x: Unknown command.", response_type="ephemeral")
                return
            await handle_tasks_command_from_parsed(
                parsed_cmd,
                ack=ack,
                body=body,
                client=client,
                respond=respond,
                store=store,
                agent_resolver=agent_resolver,
            )

    @bolt_app.view(MODAL_CALLBACK_CREATE_TASK)
    async def create_modal(ack, body, client):
        await handle_create_modal_submission(ack, body, client, store)
