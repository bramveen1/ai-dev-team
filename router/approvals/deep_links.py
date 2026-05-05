"""Deep-link URL generators for native app access.

Used by the approval flow when a draft is connector-backed (no pack):
the user can't act from Slack alone, so we render an "Open in <App>"
button that takes them straight to the resource.

The registry is keyed by ``target`` — the same string the agent puts
in the draft block's ``target`` field (e.g. ``outlook``, ``gmail``).
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import quote


def outlook_draft(draft_id: str) -> str:
    return f"https://outlook.office.com/mail/drafts/id/{quote(draft_id, safe='')}"


def outlook_calendar_event(event_id: str) -> str:
    return f"https://outlook.office.com/calendar/item/{quote(event_id, safe='')}"


def zoho_draft(draft_id: str) -> str:
    return f"https://mail.zoho.com/zm/#compose/{quote(draft_id, safe='')}"


def gmail_draft(draft_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#drafts/{quote(draft_id, safe='')}"


def google_calendar_event(event_id: str) -> str:
    return f"https://calendar.google.com/calendar/event?eid={quote(event_id, safe='')}"


def figma_file(file_id: str) -> str:
    return f"https://www.figma.com/file/{quote(file_id, safe='')}"


# Registry mapping connector ``target`` → URL generator.
# Each generator takes a resource_id string and returns a URL.
_DEEP_LINK_GENERATORS: dict[str, Callable[[str], str]] = {
    "outlook": outlook_draft,
    "outlook-calendar": outlook_calendar_event,
    "zoho": zoho_draft,
    "gmail": gmail_draft,
    "google-calendar": google_calendar_event,
    "figma": figma_file,
}


def get_deep_link(target: str, resource_id: str) -> str | None:
    """Generate a deep-link URL for a connector-backed resource.

    Returns ``None`` when no generator is registered for ``target`` —
    callers should treat that as "skip the URL" and let the button
    render without one.
    """
    generator = _DEEP_LINK_GENERATORS.get(target)
    if generator is None:
        return None
    return generator(resource_id)
