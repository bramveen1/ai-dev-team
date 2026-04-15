"""M365 Calendar MCP server — stdio-based tool server for delegate calendar access.

Exposes a restricted set of tools for reading events and proposing tentative
calendar entries via Microsoft Graph. Intentionally does NOT expose a confirm
or book tool, enforcing the propose-only trust boundary.

Tools:
  - list_events: List calendar events in a date range
  - read_event: Read a single event by ID
  - find_availability: Find free time slots for scheduling
  - create_tentative_event: Create a new tentative calendar event
  - update_event: Update an existing event
  - delete_event: Delete an event

Environment variables:
  - M365_ACCESS_TOKEN: OAuth2 access token (required)
  - M365_ACCOUNT: Calendar owner UPN for delegate access (optional, uses /me if unset)
"""

from __future__ import annotations

import logging
from typing import Any

from mcps.m365_calendar.graph_client import GraphCalendarClient

logger = logging.getLogger(__name__)

# Tool definitions — the schema exposed to the LLM via MCP.
# Explicitly: NO confirm/book tool.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_events",
        "description": (
            "List calendar events in a date/time range. Returns subject, start/end times, "
            "attendees, and status. Uses the calendarView endpoint for expanded recurring events."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_datetime": {
                    "type": "string",
                    "description": "ISO 8601 start datetime (e.g. '2026-04-20T00:00:00')",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "ISO 8601 end datetime (e.g. '2026-04-27T23:59:59')",
                },
                "top": {
                    "type": "integer",
                    "description": "Maximum number of events to return (1-100)",
                    "default": 25,
                },
            },
            "required": ["start_datetime", "end_datetime"],
        },
    },
    {
        "name": "read_event",
        "description": (
            "Read a single calendar event by ID. Returns full event details including body, attendees, and location."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The Graph event ID",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "find_availability",
        "description": (
            "Find free time slots for one or more people. Returns busy/free information "
            "and a list of available windows that meet the requested duration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schedules": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of email addresses to check availability for",
                },
                "start_datetime": {
                    "type": "string",
                    "description": "ISO 8601 start datetime for the search window",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "ISO 8601 end datetime for the search window",
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": "Minimum slot duration in minutes",
                    "default": 30,
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name (e.g. 'America/New_York')",
                    "default": "UTC",
                },
            },
            "required": ["schedules", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "create_tentative_event",
        "description": (
            "Create a new tentative calendar event. The event is always created with "
            "showAs='tentative' — the calendar owner must confirm in Outlook. "
            "This tool proposes meetings; it does not book them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Event subject/title"},
                "start_datetime": {
                    "type": "string",
                    "description": "ISO 8601 start datetime",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "ISO 8601 end datetime",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name",
                    "default": "UTC",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of attendee email addresses",
                },
                "body": {
                    "type": "string",
                    "description": "Event body/description (HTML supported)",
                },
                "location": {
                    "type": "string",
                    "description": "Event location",
                },
                "body_content_type": {
                    "type": "string",
                    "enum": ["HTML", "Text"],
                    "description": "Content type of the body",
                    "default": "HTML",
                },
            },
            "required": ["subject", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "update_event",
        "description": "Update an existing calendar event. Only provided fields are changed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The Graph event ID"},
                "subject": {"type": "string", "description": "New subject (omit to keep current)"},
                "start_datetime": {
                    "type": "string",
                    "description": "New start datetime (omit to keep current)",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "New end datetime (omit to keep current)",
                },
                "timezone": {
                    "type": "string",
                    "description": "Timezone for start/end",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New attendee list (omit to keep current)",
                },
                "body": {
                    "type": "string",
                    "description": "New body content (omit to keep current)",
                },
                "location": {
                    "type": "string",
                    "description": "New location (omit to keep current)",
                },
                "body_content_type": {
                    "type": "string",
                    "enum": ["HTML", "Text"],
                    "description": "Content type of the body",
                    "default": "HTML",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_event",
        "description": "Delete a calendar event. Use this to cancel a tentative event that is no longer needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The Graph event ID of the event to delete"},
            },
            "required": ["event_id"],
        },
    },
]


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return the list of tool definitions exposed by this MCP server."""
    return TOOL_DEFINITIONS


async def handle_tool_call(
    client: GraphCalendarClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call to the appropriate Graph client method.

    Args:
        client: Authenticated GraphCalendarClient instance.
        tool_name: Name of the tool to invoke.
        arguments: Tool arguments from the MCP request.

    Returns:
        Tool result as a dict.

    Raises:
        ValueError: If the tool name is unknown.
        GraphCalendarError: If the Graph API call fails.
    """
    if tool_name == "list_events":
        events = await client.list_events(
            start_datetime=arguments["start_datetime"],
            end_datetime=arguments["end_datetime"],
            top=arguments.get("top", 25),
            select=[
                "id",
                "subject",
                "start",
                "end",
                "organizer",
                "attendees",
                "location",
                "showAs",
                "isAllDay",
            ],
        )
        return {"events": _summarize_events(events)}

    elif tool_name == "read_event":
        event = await client.read_event(arguments["event_id"])
        return _format_event(event)

    elif tool_name == "find_availability":
        result = await client.find_availability(
            schedules=arguments["schedules"],
            start_datetime=arguments["start_datetime"],
            end_datetime=arguments["end_datetime"],
            duration_minutes=arguments.get("duration_minutes", 30),
            timezone=arguments.get("timezone", "UTC"),
        )
        return result

    elif tool_name == "create_tentative_event":
        event = await client.create_tentative_event(
            subject=arguments["subject"],
            start_datetime=arguments["start_datetime"],
            end_datetime=arguments["end_datetime"],
            timezone=arguments.get("timezone", "UTC"),
            attendees=arguments.get("attendees"),
            body=arguments.get("body"),
            location=arguments.get("location"),
            body_content_type=arguments.get("body_content_type", "HTML"),
        )
        url = await client.get_event_url(event["id"])
        return {
            "event_id": event["id"],
            "web_link": url,
            "subject": event.get("subject", ""),
            "start": event.get("start", {}),
            "end": event.get("end", {}),
            "show_as": event.get("showAs", "tentative"),
            "status": "created",
        }

    elif tool_name == "update_event":
        event = await client.update_event(
            event_id=arguments["event_id"],
            subject=arguments.get("subject"),
            start_datetime=arguments.get("start_datetime"),
            end_datetime=arguments.get("end_datetime"),
            timezone=arguments.get("timezone"),
            attendees=arguments.get("attendees"),
            body=arguments.get("body"),
            location=arguments.get("location"),
            body_content_type=arguments.get("body_content_type", "HTML"),
        )
        return {
            "event_id": event["id"],
            "subject": event.get("subject", ""),
            "status": "updated",
        }

    elif tool_name == "delete_event":
        await client.delete_event(arguments["event_id"])
        return {"event_id": arguments["event_id"], "status": "deleted"}

    else:
        raise ValueError(f"Unknown tool: {tool_name}")


def _summarize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize event list for compact tool output."""
    summaries = []
    for evt in events:
        organizer = ""
        if "organizer" in evt and evt["organizer"]:
            email_addr = evt["organizer"].get("emailAddress", {})
            organizer = email_addr.get("address", "")
            org_name = email_addr.get("name", "")
            if org_name:
                organizer = f"{org_name} <{organizer}>"

        attendee_list = []
        for att in evt.get("attendees", []):
            addr = att.get("emailAddress", {}).get("address", "")
            if addr:
                attendee_list.append(addr)

        location_name = ""
        if "location" in evt and evt["location"]:
            location_name = evt["location"].get("displayName", "")

        summaries.append(
            {
                "id": evt.get("id", ""),
                "subject": evt.get("subject", "(no subject)"),
                "start": evt.get("start", {}),
                "end": evt.get("end", {}),
                "organizer": organizer,
                "attendees": attendee_list,
                "location": location_name,
                "show_as": evt.get("showAs", ""),
                "is_all_day": evt.get("isAllDay", False),
            }
        )
    return summaries


def _format_event(event: dict[str, Any]) -> dict[str, Any]:
    """Format a full event for tool output."""
    organizer = ""
    if "organizer" in event and event["organizer"]:
        email_data = event["organizer"].get("emailAddress", {})
        organizer = email_data.get("address", "")
        org_name = email_data.get("name", "")
        if org_name:
            organizer = f"{org_name} <{organizer}>"

    attendee_list = []
    for att in event.get("attendees", []):
        addr = att.get("emailAddress", {}).get("address", "")
        status = att.get("status", {}).get("response", "")
        if addr:
            attendee_list.append({"address": addr, "response": status})

    location_name = ""
    if "location" in event and event["location"]:
        location_name = event["location"].get("displayName", "")

    body = event.get("body", {})
    return {
        "id": event.get("id", ""),
        "subject": event.get("subject", "(no subject)"),
        "start": event.get("start", {}),
        "end": event.get("end", {}),
        "organizer": organizer,
        "attendees": attendee_list,
        "location": location_name,
        "body_type": body.get("contentType", ""),
        "body": body.get("content", ""),
        "show_as": event.get("showAs", ""),
        "is_all_day": event.get("isAllDay", False),
        "is_cancelled": event.get("isCancelled", False),
        "recurrence": event.get("recurrence"),
        "web_link": event.get("webLink", ""),
    }
