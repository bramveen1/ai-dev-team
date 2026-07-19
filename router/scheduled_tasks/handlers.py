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
from typing import TYPE_CHECKING, Any, Callable

from router.scheduled_tasks.block_kit import (
    build_task_detail_message,
    build_task_list_message,
)
from router.scheduled_tasks.input_request import collect_create_task
from router.scheduled_tasks.store import ScheduledTaskStore, ScopeError

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

    from router.chat.interface import ChatAdapter
    from router.commands.types import Command, CommandResult

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
        await _handle_create_open(agent_name, body, client, respond, store)
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
    messages = build_task_list_message(agent_name, tasks)
    label = f"{agent_name.capitalize()}'s scheduled tasks"
    for message in messages:
        await respond(blocks=message["blocks"], text=label)


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


async def _handle_create_open(
    agent_name: str,
    body: dict[str, Any],
    client: Any,
    respond: Any,
    store: ScheduledTaskStore,
) -> None:
    """Run the create-task form for a slash invocation.

    The form is an :class:`~router.chat.types.InputRequest` driven through the
    Slack adapter — the adapter renders the native modal (field validation
    reprompts in-modal via ``response_action="errors"``); this caller never
    touches ``views.open`` or Block Kit (#747). This coroutine stays alive
    until the user submits, cancels, or the form times out; ``ack`` already
    ran, so Bolt is fine with the wait.
    """
    from router.chat.adapters.slack import SlackAdapter, make_inbound_ref

    trigger_id = body.get("trigger_id")
    if not trigger_id:
        await respond(text="Could not open the create task modal — missing trigger_id.")
        return

    channel = body.get("channel_id") or ""
    thread_ts = body.get("thread_ts") or body.get("message_ts") or ""
    adapter = SlackAdapter(agent_name, client, trigger_id=trigger_id)

    try:
        outcome = await collect_create_task(adapter, make_inbound_ref(channel, thread_ts), agent_name, store)
    except Exception:
        logger.exception("Failed to open create task modal for agent=%s", agent_name)
        await respond(text="Sorry, I couldn't open the task creation modal.")
        return

    # Closing the modal ("Cancel") stays silent, matching the legacy modal.
    if outcome.message:
        await respond(text=outcome.message)


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


async def execute_tasks_command(
    cmd: "Command",
    *,
    subject_agent: str | None = None,
    store: "ScheduledTaskStore | None" = None,
    adapter: "ChatAdapter | None" = None,
    conversation_ref: str | None = None,
) -> "CommandResult":
    """Transport-neutral ``tasks`` verb handler returning a :class:`~router.commands.types.CommandResult`.

    Supports ``tasks list`` (read-only) on any transport. ``tasks create``
    additionally needs an ``adapter`` + ``conversation_ref`` to drive the
    create-task :class:`~router.chat.types.InputRequest`: form-capable
    transports render their native form, everything else falls back to
    scripted Q&A (#747). Without an adapter the create subcommand returns an
    informational error.
    """
    from router.chat.types import ConversationRef
    from router.commands.types import CommandResult

    if store is None:
        return CommandResult(text="tasks store not configured", ok=False)

    if subject_agent is None:
        return CommandResult(
            text="error: could not determine agent context for tasks command",
            ok=False,
        )

    subcommand = cmd.args[0].lower() if cmd.args else "list"

    if subcommand == "list":
        tasks = store.list_for_agent(subject_agent)
        if not tasks:
            return CommandResult(text=f"No scheduled tasks for {subject_agent}.")
        lines = [f"Scheduled tasks for {subject_agent.capitalize()}:"]
        for task in tasks:
            state_label = "enabled" if task.enabled else "paused"
            lines.append(f"  [{state_label}] {task.name} ({task.task_id}) — {task.schedule_cron}")
        return CommandResult(text="\n".join(lines))

    if subcommand == "create":
        if adapter is None or conversation_ref is None:
            return CommandResult(
                text="tasks create requires an interactive session — this surface provides no input transport",
                ok=False,
            )
        outcome = await collect_create_task(adapter, ConversationRef(conversation_ref), subject_agent, store)
        return CommandResult(
            text=outcome.message or "Task creation cancelled.",
            ok=outcome.status == "created",
        )

    return CommandResult(
        text=f"Unknown subcommand '{subcommand}'. Try: list.",
        ok=False,
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

    ``tasks create`` runs the create-task ``InputRequest`` through the Slack
    adapter, which renders it as a native modal (#747).
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
        await _handle_create_open(agent_name, body, client, respond, store)
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
    """Register the scheduled-tasks slash command + the shared InputRequest modal handlers.

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
    from router.chat.adapters.slack_forms import register_input_request_handlers
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

    # The create-task modal is a generic InputRequest form now — one shared
    # pair of view_submission/view_closed listeners serves every collector.
    register_input_request_handlers(bolt_app)
