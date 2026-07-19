"""Transport-neutral create-task collector, expressed as an ``InputRequest`` (#747).

The single definition of the create-task form — field set, validation rules,
and task construction — shared by every surface:

* the Slack ``/tasks create`` slash path (``handlers._handle_create_open``),
  where a form-capable adapter renders it as a native modal;
* the transport-neutral ``tasks create`` verb (``handlers.execute_tasks_command``),
  where a no-modal transport fulfils it via scripted Q&A.

No Slack view/Block Kit construction happens here or in any caller; the Slack
rendering lives entirely in ``router.chat.adapters.slack_forms``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from router.chat.input_collect import collect_input_scripted
from router.chat.interface import ChatAdapter
from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest
from router.scheduled_tasks import cron
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore

logger = logging.getLogger(__name__)

TIMEOUT_MIN = 60
TIMEOUT_MAX = 7200

# How long the form stays open before the collector gives up. The legacy
# modal had no expiry at all; 15 minutes keeps the "fill it in when you get
# to it" feel without leaking pending futures forever.
CREATE_TASK_FORM_TIMEOUT_SECONDS = 900


def _cron_valid(value: str) -> bool:
    """Field validator — ``CronError``'s message becomes the field error text."""
    cron.validate(value)
    return True


def _timeout_in_range(value: str) -> bool:
    return TIMEOUT_MIN <= int(value) <= TIMEOUT_MAX


def build_create_task_request(agent_name: str) -> InputRequest:
    """The create-task form: name, prompt, cron, destination, timeout."""
    return InputRequest(
        title=f"New task for {agent_name.capitalize()}",
        fields=[
            InputField(key="name", label="Name"),
            InputField(key="prompt", label="Prompt", multiline=True),
            InputField(key="schedule_cron", label="Cron schedule (5 fields, UTC)", validator=_cron_valid),
            InputField(key="destination", label="Destination", type=InputFieldType.CONVERSATION),
            InputField(
                key="timeout_seconds",
                label=f"Timeout in seconds ({TIMEOUT_MIN}-{TIMEOUT_MAX})",
                type=InputFieldType.INT,
                required=False,
                validator=_timeout_in_range,
            ),
        ],
        timeout_seconds=CREATE_TASK_FORM_TIMEOUT_SECONDS,
    )


@dataclass
class CreateTaskOutcome:
    """Result of one create-task collection attempt.

    ``message`` is user-facing; it is empty for ``cancelled`` so the Slack
    caller can keep the legacy behavior of saying nothing when the user just
    closes the modal.
    """

    task: ScheduledTask | None
    status: str  # "created" | "error" | "cancelled" | "timed_out"
    message: str


def create_task_from_values(agent_name: str, values: dict[str, str], store: ScheduledTaskStore) -> CreateTaskOutcome:
    """Build and persist a :class:`ScheduledTask` from validated form values."""
    schedule_cron = values.get("schedule_cron", "")
    now = datetime.now(timezone.utc)
    try:
        next_run = cron.next_run_after(schedule_cron, now)
    except cron.CronError as e:
        # validate() passed but the schedule has no next firing — surface it
        # rather than storing a task that can never run.
        return CreateTaskOutcome(task=None, status="error", message=f"Could not schedule the task: {e}")

    timeout_raw = values.get("timeout_seconds", "")
    task = ScheduledTask(
        task_id=str(uuid.uuid4()),
        agent_name=agent_name,
        name=values.get("name", ""),
        prompt=values.get("prompt", ""),
        schedule_cron=schedule_cron,
        destination=values.get("destination") or None,
        enabled=True,
        created_at=now,
        next_run_at=next_run,
        timeout_seconds=int(timeout_raw) if timeout_raw else None,
    )
    store.create(task)

    confirmation = (
        f"Created *{task.name}* for {task.agent_name.capitalize()}.\n"
        f"• Schedule: `{task.schedule_cron}`\n"
        f"• Next run: {task.next_run_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"• Destination: `{task.destination}`\n"
        f"• Task ID: `{task.task_id}`"
    )
    return CreateTaskOutcome(task=task, status="created", message=confirmation)


async def collect_create_task(
    adapter: ChatAdapter,
    conversation_ref: ConversationRef,
    agent_name: str,
    store: ScheduledTaskStore,
) -> CreateTaskOutcome:
    """Drive the create-task ``InputRequest`` over ``adapter`` and persist the result.

    Form-capable transports get their native form (Slack: modal, including
    in-modal validation reprompts); everything else inherits the scripted
    Q&A fallback with the same field rules.
    """
    request = build_create_task_request(agent_name)
    if adapter.capabilities.supports_forms:
        response = await adapter.collect_input(conversation_ref, request)
    else:
        response = await collect_input_scripted(adapter, conversation_ref, request)

    if response.status == "cancelled":
        return CreateTaskOutcome(task=None, status="cancelled", message="")
    if response.status == "timed_out":
        return CreateTaskOutcome(task=None, status="timed_out", message="Task creation timed out — nothing was saved.")
    return create_task_from_values(agent_name, response.values, store)
