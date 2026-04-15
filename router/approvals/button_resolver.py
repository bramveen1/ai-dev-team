"""Permission-aware button resolver for approval flow.

Computes the correct set of action buttons based on the capability
instance's permissions. If the agent has direct action permission
(e.g. 'send'), show an action button. Otherwise, show a deep-link
"Open in {app}" button for the user to act in the native app.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_OPEN_IN_APP,
    ACTION_REQUEST_EDIT,
)

if TYPE_CHECKING:
    from capabilities.models import CapabilityInstance


@dataclass
class ButtonSpec:
    """Specification for a single approval button."""

    action_id: str
    text: str
    style: str = "default"  # "primary", "danger", or "default"
    url: str | None = None


# Maps (capability_type, action_verb) to the approval action_id
_VERB_ACTION_MAP: dict[str, str] = {
    "send": ACTION_APPROVE_SEND,
    "publish": ACTION_APPROVE_PUBLISH,
    "book": ACTION_APPROVE_BOOK,
}

# Maps capability_type to the human-readable app name for "Open in {app}" buttons
_APP_NAMES: dict[str, dict[str, str]] = {
    "email": {
        "m365-mcp": "Outlook",
        "zoho-mcp": "Zoho Mail",
        "gmail-mcp": "Gmail",
    },
    "social": {
        "buffer-mcp": "Buffer",
        "twitter-mcp": "Twitter",
        "linkedin-mcp": "LinkedIn",
    },
    "calendar": {
        "m365-mcp": "Outlook Calendar",
        "google-calendar-mcp": "Google Calendar",
    },
    "design": {
        "figma-mcp": "Figma",
    },
}


def _get_app_name(capability_type: str, provider: str) -> str:
    """Look up the human-readable app name for a provider."""
    type_map = _APP_NAMES.get(capability_type, {})
    return type_map.get(provider, provider)


def resolve_buttons(
    capability_type: str,
    capability_instance: CapabilityInstance,
    action_verb: str,
    deep_link_url: str | None = None,
) -> list[ButtonSpec]:
    """Compute the button set for a draft approval message.

    If the action_verb is in the instance's permissions, show the direct
    action button (Send/Publish/Book). Otherwise, show an "Open in {app}"
    link button.

    In both cases, Edit and Discard buttons are included.

    Args:
        capability_type: The capability type (email, social, calendar, etc.)
        capability_instance: The CapabilityInstance with permissions list.
        action_verb: The verb the agent wants to perform (send, publish, book).
        deep_link_url: Optional URL for the "Open in {app}" button.

    Returns:
        Ordered list of ButtonSpec objects.
    """
    buttons: list[ButtonSpec] = []
    has_permission = action_verb in capability_instance.permissions

    if has_permission:
        # Agent can execute directly — show the action button
        approve_action = _VERB_ACTION_MAP.get(action_verb, ACTION_APPROVE_SEND)
        buttons.append(
            ButtonSpec(
                action_id=approve_action,
                text=action_verb.capitalize(),
                style="primary",
            )
        )
        buttons.append(
            ButtonSpec(
                action_id=ACTION_REQUEST_EDIT,
                text="Edit",
                style="default",
            )
        )
    else:
        # Agent can only draft — show "Open in {app}" link + Redraft
        app_name = _get_app_name(capability_type, capability_instance.provider)
        buttons.append(
            ButtonSpec(
                action_id=ACTION_OPEN_IN_APP,
                text=f"Open in {app_name}",
                style="primary",
                url=deep_link_url,
            )
        )
        buttons.append(
            ButtonSpec(
                action_id=ACTION_REQUEST_EDIT,
                text="Redraft",
                style="default",
            )
        )

    buttons.append(
        ButtonSpec(
            action_id=ACTION_DISCARD,
            text="Discard",
            style="danger",
        )
    )

    return buttons
