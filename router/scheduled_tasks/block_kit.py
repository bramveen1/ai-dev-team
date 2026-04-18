"""Block Kit message and modal builders for the /tasks slash command.

Produces JSON payloads used by :mod:`router.scheduled_tasks.handlers`:
    - ``build_task_list_message``: renders an agent's tasks in a Slack message
    - ``build_create_task_modal``: the modal shown by ``/tasks create``
    - ``parse_create_modal_submission``: extracts values from the modal submit
"""

from __future__ import annotations

from typing import Any

from router.scheduled_tasks.store import ScheduledTask

MODAL_CALLBACK_CREATE_TASK = "scheduled_tasks_create_modal"

BLOCK_ID_NAME = "task_name"
BLOCK_ID_PROMPT = "task_prompt"
BLOCK_ID_CRON = "task_cron"
BLOCK_ID_DESTINATION = "task_destination"

ACTION_ID_NAME = "name_input"
ACTION_ID_PROMPT = "prompt_input"
ACTION_ID_CRON = "cron_input"
ACTION_ID_DESTINATION = "destination_input"


def _format_task_line(task: ScheduledTask) -> str:
    status = "paused" if not task.enabled else "active"
    last = task.last_run_at.strftime("%Y-%m-%d %H:%M UTC") if task.last_run_at else "never"
    next_ = task.next_run_at.strftime("%Y-%m-%d %H:%M UTC")
    dest = task.destination or "agent DM"
    return (
        f"*{task.name}* — `{task.schedule_cron}` ({status})\n"
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
                "optional": True,
                "label": {"type": "plain_text", "text": "Destination channel ID (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": ACTION_ID_DESTINATION,
                    "placeholder": {"type": "plain_text", "text": "C0123456789"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": "Leave blank to post to the agent's DM with Bram.",
                },
            },
        ],
    }


def parse_create_modal_submission(view: dict[str, Any]) -> dict[str, Any]:
    """Extract values from a ``view_submission`` payload for the create modal.

    Returns a dict with keys: ``agent_name``, ``name``, ``prompt``, ``schedule_cron``,
    ``destination``. Missing optional fields come back as empty strings / None.
    """
    state = view.get("state", {}).get("values", {})
    agent_name = view.get("private_metadata", "")

    def _value(block_id: str, action_id: str) -> str:
        return (state.get(block_id, {}).get(action_id, {}) or {}).get("value", "") or ""

    destination_raw = _value(BLOCK_ID_DESTINATION, ACTION_ID_DESTINATION).strip()
    return {
        "agent_name": agent_name,
        "name": _value(BLOCK_ID_NAME, ACTION_ID_NAME).strip(),
        "prompt": _value(BLOCK_ID_PROMPT, ACTION_ID_PROMPT).strip(),
        "schedule_cron": _value(BLOCK_ID_CRON, ACTION_ID_CRON).strip(),
        "destination": destination_raw or None,
    }
