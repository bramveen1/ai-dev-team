"""Block Kit message and modal builders for the /tasks slash command.

Produces JSON payloads used by :mod:`router.scheduled_tasks.handlers`:
    - ``build_task_list_message``: renders an agent's tasks in a Slack message
    - ``build_create_task_modal``: the modal shown by ``/tasks create``
    - ``parse_create_modal_submission``: extracts values from the modal submit
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from router.scheduled_tasks.store import ScheduledTask

MODAL_CALLBACK_CREATE_TASK = "scheduled_tasks_create_modal"

BLOCK_ID_NAME = "task_name"
BLOCK_ID_PROMPT = "task_prompt"
BLOCK_ID_CRON = "task_cron"
BLOCK_ID_DESTINATION = "task_destination"
BLOCK_ID_TIMEOUT = "task_timeout"

ACTION_ID_NAME = "name_input"
ACTION_ID_PROMPT = "prompt_input"
ACTION_ID_CRON = "cron_input"
ACTION_ID_DESTINATION = "destination_input"
ACTION_ID_TIMEOUT = "timeout_input"

TIMEOUT_MIN = 60
TIMEOUT_MAX = 7200


def _format_fires_in(next_run_at: datetime) -> str:
    """Human-readable relative time until ``next_run_at``."""
    now = datetime.now(timezone.utc)
    delta = next_run_at - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "overdue"
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return f"{seconds}s"


def _format_task_line(task: ScheduledTask) -> str:
    status = "paused" if not task.enabled else "active"
    last = task.last_run_at.strftime("%Y-%m-%d %H:%M UTC") if task.last_run_at else "never"
    next_ = task.next_run_at.strftime("%Y-%m-%d %H:%M UTC")
    # Destination is required at create time, but old rows from before that
    # change may still be null — surface that explicitly so it's clear the
    # task can't post anywhere yet.
    dest = f"<#{task.destination}>" if task.destination else "_unset_"
    if task.one_shot:
        schedule_display = f"(one-shot, fires in {_format_fires_in(task.next_run_at)})"
    else:
        schedule_display = f"`{task.schedule_cron}`"
    return (
        f"*{task.name}* — {schedule_display} ({status})\n"
        f"    task_id: `{task.task_id}`\n"
        f"    destination: {dest}\n"
        f"    last run: {last} · next run: {next_}"
    )


def build_task_list_message(agent_name: str, tasks: list[ScheduledTask]) -> dict[str, Any]:
    """Render the reply to ``/tasks list``."""
    agent_display = agent_name.capitalize()
    if not tasks:
        return {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{agent_display}* has no scheduled tasks. Create one with `/tasks create`.",
                    },
                },
            ]
        }

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{agent_display}'s scheduled tasks"},
        },
    ]
    for task in tasks:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_task_line(task)},
            }
        )
        blocks.append({"type": "divider"})
    return {"blocks": blocks}


def build_task_detail_message(task: ScheduledTask) -> dict[str, Any]:
    """Render the reply to ``/tasks detail <task_id>``."""
    status = "active" if task.enabled else "paused"
    next_run = task.next_run_at.strftime("%Y-%m-%d %H:%M UTC")
    last_run = task.last_run_at.strftime("%Y-%m-%d %H:%M UTC") if task.last_run_at else "never"
    dest = f"<#{task.destination}>" if task.destination else "_unset_"
    if task.one_shot:
        schedule_display = f"(one-shot, fires in {_format_fires_in(task.next_run_at)})"
    else:
        schedule_display = f"`{task.schedule_cron}`"
    timeout = str(task.timeout_seconds) if task.timeout_seconds is not None else "default"

    summary = (
        f"*{task.name}*\n"
        f"• task_id: `{task.task_id}`\n"
        f"• schedule: {schedule_display}\n"
        f"• status: {status}\n"
        f"• destination: {dest}\n"
        f"• next run: {next_run}\n"
        f"• last run: {last_run}\n"
        f"• timeout: {timeout}s"
    )
    return {
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Prompt:*\n```{task.prompt}```",
                },
            },
        ]
    }


def build_create_task_modal(agent_name: str) -> dict[str, Any]:
    """Return the Slack view payload for the ``/tasks create`` modal."""
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_CREATE_TASK,
        "private_metadata": agent_name,
        "title": {"type": "plain_text", "text": f"New task for {agent_name.capitalize()}"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": BLOCK_ID_NAME,
                "label": {"type": "plain_text", "text": "Name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": ACTION_ID_NAME,
                    "placeholder": {"type": "plain_text", "text": "Daily inbox review"},
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_ID_PROMPT,
                "label": {"type": "plain_text", "text": "Prompt"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": ACTION_ID_PROMPT,
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Summarize yesterday's inbox activity and post the highlights.",
                    },
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_ID_CRON,
                "label": {"type": "plain_text", "text": "Cron schedule (5 fields, UTC)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": ACTION_ID_CRON,
                    "placeholder": {"type": "plain_text", "text": "0 9 * * 1-5"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": "minute hour day-of-month month day-of-week",
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_ID_DESTINATION,
                "label": {"type": "plain_text", "text": "Destination"},
                "element": {
                    "type": "conversations_select",
                    "action_id": ACTION_ID_DESTINATION,
                    "default_to_current_conversation": True,
                    "filter": {"include": ["public", "private", "im"]},
                    "placeholder": {"type": "plain_text", "text": "Pick a channel or DM"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": "The agent's reply will be posted here every time the task runs.",
                },
            },
            {
                "type": "input",
                "block_id": BLOCK_ID_TIMEOUT,
                "label": {"type": "plain_text", "text": "Timeout (seconds)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": ACTION_ID_TIMEOUT,
                    "placeholder": {"type": "plain_text", "text": "Default: 300"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": f"How long to let the task run before cancelling it ({TIMEOUT_MIN}–{TIMEOUT_MAX}s).",
                },
            },
        ],
    }


def build_create_task_confirmation_view(task: ScheduledTask) -> dict[str, Any]:
    """Modal view shown after a successful create-task submission.

    Returned via ``response_action: "update"`` from the view-submission handler
    so Slack swaps the form with this confirmation in-place — no chat-postMessage
    follow-up needed (which would otherwise require the bot's Messages tab to
    be enabled).
    """
    next_run = task.next_run_at.strftime("%Y-%m-%d %H:%M UTC")
    destination = f"<#{task.destination}>" if task.destination else "_unset_"
    return {
        "type": "modal",
        "callback_id": f"{MODAL_CALLBACK_CREATE_TASK}_done",
        "title": {"type": "plain_text", "text": "Task created"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Created *{task.name}* for {task.agent_name.capitalize()}.\n"
                        f"• Schedule: `{task.schedule_cron}`\n"
                        f"• Next run: {next_run}\n"
                        f"• Destination: {destination}\n"
                        f"• Task ID: `{task.task_id}`"
                    ),
                },
            },
        ],
    }


def parse_create_modal_submission(view: dict[str, Any]) -> dict[str, Any]:
    """Extract values from a ``view_submission`` payload for the create modal.

    Returns a dict with keys: ``agent_name``, ``name``, ``prompt``, ``schedule_cron``,
    ``destination``, ``timeout_seconds``. The destination field comes from a
    ``conversations_select`` element, so the value is the picked conversation ID rather
    than free text. ``timeout_seconds`` is ``None`` when the field is left blank.
    """
    state = view.get("state", {}).get("values", {})
    agent_name = view.get("private_metadata", "")

    def _value(block_id: str, action_id: str) -> str:
        return (state.get(block_id, {}).get(action_id, {}) or {}).get("value", "") or ""

    destination_block = state.get(BLOCK_ID_DESTINATION, {}).get(ACTION_ID_DESTINATION, {}) or {}
    destination = (destination_block.get("selected_conversation") or "").strip()

    timeout_raw = _value(BLOCK_ID_TIMEOUT, ACTION_ID_TIMEOUT).strip()
    timeout_seconds: int | None
    if not timeout_raw:
        timeout_seconds = None
    else:
        try:
            timeout_seconds = int(timeout_raw)
        except ValueError:
            timeout_seconds = -1  # sentinel: caller validates and emits the block error

    return {
        "agent_name": agent_name,
        "name": _value(BLOCK_ID_NAME, ACTION_ID_NAME).strip(),
        "prompt": _value(BLOCK_ID_PROMPT, ACTION_ID_PROMPT).strip(),
        "schedule_cron": _value(BLOCK_ID_CRON, ACTION_ID_CRON).strip(),
        "destination": destination or None,
        "timeout_seconds": timeout_seconds,
    }
