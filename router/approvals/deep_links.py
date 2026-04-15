"""Deep link URL generators for native app access.

Generates URLs that open external resources in their native apps
(Outlook, Zoho, Figma, etc.) so users can act on drafts directly
when the agent doesn't have direct-action permission.
"""

from __future__ import annotations

from urllib.parse import quote


def outlook_draft(draft_id: str) -> str:
    """Generate a deep link to an Outlook draft message.

    Uses the Outlook web app URL scheme.
    """
    return f"https://outlook.office.com/mail/drafts/id/{quote(draft_id, safe='')}"


def zoho_draft(draft_id: str) -> str:
    """Generate a deep link to a Zoho Mail draft."""
    return f"https://mail.zoho.com/zm/#compose/{quote(draft_id, safe='')}"


def figma_file(file_id: str) -> str:
    """Generate a deep link to a Figma file."""
    return f"https://www.figma.com/file/{quote(file_id, safe='')}"


def google_calendar_event(event_id: str) -> str:
    """Generate a deep link to a Google Calendar event."""
    return f"https://calendar.google.com/calendar/event?eid={quote(event_id, safe='')}"


def outlook_calendar_event(event_id: str) -> str:
    """Generate a deep link to an Outlook Calendar event."""
    return f"https://outlook.office.com/calendar/item/{quote(event_id, safe='')}"


# Registry mapping (capability_type, provider) to the URL generator function.
# Each function takes a resource_id string and returns a URL.
DEEP_LINK_GENERATORS: dict[tuple[str, str], callable] = {
    ("email", "m365-mcp"): outlook_draft,
    ("email", "zoho-mcp"): zoho_draft,
    ("design", "figma-mcp"): figma_file,
    ("calendar", "google-calendar-mcp"): google_calendar_event,
    ("calendar", "m365-mcp"): outlook_calendar_event,
}


def get_deep_link(capability_type: str, provider: str, resource_id: str) -> str | None:
    """Look up and generate a deep link URL for a given resource.

    Returns None if no generator is registered for the (capability_type, provider) pair.
    """
    generator = DEEP_LINK_GENERATORS.get((capability_type, provider))
    if generator is None:
        return None
    return generator(resource_id)
