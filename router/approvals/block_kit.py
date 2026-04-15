"""Block Kit message builder for approval flow drafts.

Builds Slack Block Kit JSON payloads for draft approval messages,
including header, content preview, and action buttons.
"""

from __future__ import annotations

from typing import Any

from router.approvals.store import Draft

# Standard action_id vocabulary
ACTION_APPROVE_SEND = "approve_send"
ACTION_APPROVE_PUBLISH = "approve_publish"
ACTION_APPROVE_BOOK = "approve_book"
ACTION_REQUEST_EDIT = "request_edit"
ACTION_DISCARD = "discard"
ACTION_OPEN_IN_APP = "open_in_app"

# Maps action verbs to their approval action_id
VERB_TO_APPROVE_ACTION: dict[str, str] = {
    "send": ACTION_APPROVE_SEND,
    "publish": ACTION_APPROVE_PUBLISH,
    "book": ACTION_APPROVE_BOOK,
}

# Button display configuration
BUTTON_CONFIG: dict[str, dict[str, str]] = {
    ACTION_APPROVE_SEND: {"text": "Send", "style": "primary"},
    ACTION_APPROVE_PUBLISH: {"text": "Publish", "style": "primary"},
    ACTION_APPROVE_BOOK: {"text": "Book", "style": "primary"},
    ACTION_REQUEST_EDIT: {"text": "Edit", "style": "default"},
    ACTION_DISCARD: {"text": "Discard", "style": "danger"},
}


def _format_payload_preview(draft: Draft) -> str:
    """Format the draft payload into a human-readable preview string."""
    payload = draft.payload
    parts: list[str] = []

    if "to" in payload:
        parts.append(f"*To:* {payload['to']}")
    if "subject" in payload:
        parts.append(f"*Subject:* {payload['subject']}")
    if "body" in payload:
        body = payload["body"]
        if len(body) > 300:
            body = body[:300] + "..."
        parts.append(f"*Content:*\n{body}")
    if "title" in payload:
        parts.append(f"*Title:* {payload['title']}")
    if "content" in payload:
        content = payload["content"]
        if len(content) > 300:
            content = content[:300] + "..."
        parts.append(f"*Content:*\n{content}")
    if "attendees" in payload:
        parts.append(f"*Attendees:* {', '.join(payload['attendees'])}")
    if "start_time" in payload:
        parts.append(f"*When:* {payload['start_time']}")

    if not parts:
        parts.append(f"```{str(payload)[:300]}```")

    return "\n".join(parts)


def _make_button(action_id: str, draft_id: str, url: str | None = None) -> dict[str, Any]:
    """Build a single Block Kit button element."""
    config = BUTTON_CONFIG.get(action_id, {"text": action_id, "style": "default"})
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": config["text"]},
        "action_id": action_id,
        "value": draft_id,
    }
    if config["style"] == "primary":
        button["style"] = "primary"
    elif config["style"] == "danger":
        button["style"] = "danger"

    if url:
        button["url"] = url

    return button


def _make_button_from_spec(spec: Any, draft_id: str) -> dict[str, Any]:
    """Build a Block Kit button element from a ButtonSpec."""
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": spec.text},
        "action_id": spec.action_id,
        "value": draft_id,
    }
    if spec.style == "primary":
        button["style"] = "primary"
    elif spec.style == "danger":
        button["style"] = "danger"

    if spec.url:
        button["url"] = spec.url

    return button


def _build_base_blocks(draft: Draft) -> list[dict[str, Any]]:
    """Build the common header/context/content blocks for an approval message."""
    agent_display = draft.agent_name.capitalize()
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{agent_display} wants to {draft.action_verb}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"via *{draft.capability_instance}* ({draft.capability_type})",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _format_payload_preview(draft),
            },
        },
        {"type": "divider"},
    ]


def build_approval_message(draft: Draft, buttons: list[str]) -> dict[str, Any]:
    """Build a Block Kit message for a draft approval request.

    Args:
        draft: The draft to build the message for.
        buttons: List of action_id strings for the buttons to include.

    Returns:
        A dict with 'blocks' key containing Block Kit blocks, suitable
        for passing to chat.postMessage or chat.update.
    """
    blocks = _build_base_blocks(draft)

    # Build action buttons
    button_elements = [_make_button(action_id, draft.draft_id) for action_id in buttons]
    if button_elements:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"approval_{draft.draft_id}",
                "elements": button_elements,
            }
        )

    return {"blocks": blocks}


def build_approval_message_from_specs(draft: Draft, button_specs: list[Any]) -> dict[str, Any]:
    """Build a Block Kit message using ButtonSpec objects from the button resolver.

    This is the permission-aware variant of build_approval_message.
    Instead of raw action_id strings, it accepts ButtonSpec objects that
    include custom text, style, and optional URL (for deep links).

    Args:
        draft: The draft to build the message for.
        button_specs: List of ButtonSpec objects from resolve_buttons().

    Returns:
        A dict with 'blocks' key containing Block Kit blocks.
    """
    blocks = _build_base_blocks(draft)

    button_elements = [_make_button_from_spec(spec, draft.draft_id) for spec in button_specs]
    if button_elements:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"approval_{draft.draft_id}",
                "elements": button_elements,
            }
        )

    return {"blocks": blocks}


def build_outcome_message(draft: Draft, approved: bool) -> dict[str, Any]:
    """Build a Block Kit message showing the outcome of an approval decision.

    Replaces the original approval message after a user acts on it.

    Args:
        draft: The draft that was acted upon (with updated status).
        approved: True if the action was approved, False if discarded.

    Returns:
        A dict with 'blocks' key containing the outcome message.
    """
    if approved:
        resolved_time = draft.resolved_at.strftime("%I:%M %p") if draft.resolved_at else "just now"
        status_text = f":white_check_mark: {draft.action_verb.capitalize()}ed at {resolved_time}"
    else:
        status_text = ":x: Discarded"

    agent_display = draft.agent_name.capitalize()

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{agent_display} — {draft.action_verb}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"via *{draft.capability_instance}* ({draft.capability_type})",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": status_text,
            },
        },
    ]

    return {"blocks": blocks}
