"""Block Kit message builders for the /tasks slash command.

Produces JSON payloads used by :mod:`router.scheduled_tasks.handlers`:
    - ``build_task_list_message``: renders an agent's tasks in a Slack message
    - ``build_task_detail_message``: renders one task for ``/tasks detail``

The ``/tasks create`` modal is no longer built here — the create-task form is
a transport-neutral :class:`~router.chat.types.InputRequest`
(:mod:`router.scheduled_tasks.input_request`) rendered generically by
:mod:`router.chat.adapters.slack_forms` (#747).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from router.scheduled_tasks.store import ScheduledTask

SLACK_MAX_BLOCKS = 50


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


def build_task_list_message(agent_name: str, tasks: list[ScheduledTask]) -> list[dict[str, Any]]:
    """Render the reply to ``/tasks list`` as a list of ≤50-block Slack messages.

    Each task occupies 2 blocks (section + divider). The first chunk also
    carries a header block, so it holds at most 24 tasks. Subsequent chunks
    hold at most 25 tasks each.  This keeps every ``respond(blocks=...)`` call
    within Slack's hard 50-block-per-message limit regardless of how many tasks
    the agent holds (store ceiling: MAX_PENDING_TASKS_PER_AGENT = 50).
    """
    agent_display = agent_name.capitalize()
    if not tasks:
        return [
            {
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
        ]

    header: dict[str, Any] = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"{agent_display}'s scheduled tasks"},
    }

    messages: list[dict[str, Any]] = []
    current_blocks: list[dict[str, Any]] = [header]

    for task in tasks:
        task_blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _format_task_line(task)},
            },
            {"type": "divider"},
        ]
        if len(current_blocks) + len(task_blocks) > SLACK_MAX_BLOCKS:
            messages.append({"blocks": current_blocks})
            current_blocks = []
        current_blocks.extend(task_blocks)

    if current_blocks:
        messages.append({"blocks": current_blocks})

    return messages


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
