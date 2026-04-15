"""Tests for the M365 Calendar MCP server tool definitions and handler.

Verifies:
- Tool definitions include all required tools and NO confirm/book tool
- Tool handler dispatches correctly to GraphCalendarClient methods
- Tool output is formatted correctly
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcps.m365_calendar.server import (
    TOOL_DEFINITIONS,
    _format_event,
    _summarize_events,
    get_tool_definitions,
    handle_tool_call,
)

pytestmark = pytest.mark.unit


class TestToolDefinitions:
    """Verify the tool surface area is correct — especially that confirm/book is absent."""

    def test_expected_tools_present(self):
        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "list_events",
            "read_event",
            "find_availability",
            "create_tentative_event",
            "update_event",
            "delete_event",
        }
        assert tool_names == expected

    def test_no_confirm_or_book_tool(self):
        """Enforce the propose-only trust boundary at the tool level."""
        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        forbidden = {n for n in tool_names if any(w in n.lower() for w in ["confirm", "book", "accept"])}
        assert forbidden == set(), f"Found confirm/book-like tools: {forbidden}"

    def test_get_tool_definitions_returns_same(self):
        assert get_tool_definitions() == TOOL_DEFINITIONS

    def test_all_tools_have_input_schema(self):
        for tool in TOOL_DEFINITIONS:
            assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"
            assert tool["inputSchema"]["type"] == "object"

    def test_create_tentative_event_requires_subject_and_times(self):
        create = next(t for t in TOOL_DEFINITIONS if t["name"] == "create_tentative_event")
        required = create["inputSchema"]["required"]
        assert "subject" in required
        assert "start_datetime" in required
        assert "end_datetime" in required

    def test_find_availability_requires_schedules_and_times(self):
        find = next(t for t in TOOL_DEFINITIONS if t["name"] == "find_availability")
        required = find["inputSchema"]["required"]
        assert "schedules" in required
        assert "start_datetime" in required
        assert "end_datetime" in required

    def test_create_tentative_event_description_mentions_tentative(self):
        create = next(t for t in TOOL_DEFINITIONS if t["name"] == "create_tentative_event")
        assert "tentative" in create["description"].lower()


class TestHandleToolCall:
    """Verify tool call dispatch to the GraphCalendarClient."""

    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        client.list_events.return_value = [
            {
                "id": "evt1",
                "subject": "Team Standup",
                "start": {"dateTime": "2026-04-21T09:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-04-21T09:30:00", "timeZone": "UTC"},
                "organizer": {"emailAddress": {"name": "Bram", "address": "bram@example.com"}},
                "attendees": [{"emailAddress": {"address": "alice@example.com"}}],
                "location": {"displayName": "Room A"},
                "showAs": "busy",
                "isAllDay": False,
            }
        ]
        client.read_event.return_value = {
            "id": "evt1",
            "subject": "Team Standup",
            "start": {"dateTime": "2026-04-21T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-21T09:30:00", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": "Bram", "address": "bram@example.com"}},
            "attendees": [
                {
                    "emailAddress": {"address": "alice@example.com"},
                    "status": {"response": "accepted"},
                }
            ],
            "location": {"displayName": "Room A"},
            "body": {"contentType": "HTML", "content": "<p>Daily standup</p>"},
            "showAs": "busy",
            "isAllDay": False,
            "isCancelled": False,
            "recurrence": None,
            "webLink": "https://outlook.office.com/calendar/item/evt1",
        }
        client.find_availability.return_value = {
            "schedules": [{"scheduleId": "bram@example.com", "availabilityView": "0022000"}],
            "free_slots": [
                {"start": "2026-04-21T09:00:00", "end": "2026-04-21T10:00:00"},
                {"start": "2026-04-21T11:00:00", "end": "2026-04-21T14:30:00"},
            ],
        }
        client.create_tentative_event.return_value = {
            "id": "evt_new",
            "subject": "Proposed Meeting",
            "start": {"dateTime": "2026-04-22T14:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-22T14:30:00", "timeZone": "UTC"},
            "showAs": "tentative",
        }
        client.get_event_url.return_value = "https://outlook.office.com/calendar/item/evt_new"
        client.update_event.return_value = {"id": "evt_new", "subject": "Updated Meeting"}
        client.delete_event.return_value = None
        return client

    @pytest.mark.asyncio
    async def test_list_events(self, mock_client):
        result = await handle_tool_call(
            mock_client,
            "list_events",
            {"start_datetime": "2026-04-21T00:00:00", "end_datetime": "2026-04-27T23:59:59", "top": 10},
        )

        mock_client.list_events.assert_awaited_once()
        assert "events" in result
        assert len(result["events"]) == 1
        assert result["events"][0]["subject"] == "Team Standup"
        assert "Bram" in result["events"][0]["organizer"]

    @pytest.mark.asyncio
    async def test_read_event(self, mock_client):
        result = await handle_tool_call(mock_client, "read_event", {"event_id": "evt1"})

        mock_client.read_event.assert_awaited_once_with("evt1")
        assert result["subject"] == "Team Standup"
        assert result["attendees"][0]["address"] == "alice@example.com"
        assert result["attendees"][0]["response"] == "accepted"

    @pytest.mark.asyncio
    async def test_find_availability(self, mock_client):
        result = await handle_tool_call(
            mock_client,
            "find_availability",
            {
                "schedules": ["bram@example.com"],
                "start_datetime": "2026-04-21T09:00:00",
                "end_datetime": "2026-04-21T17:00:00",
                "duration_minutes": 30,
            },
        )

        mock_client.find_availability.assert_awaited_once()
        assert "free_slots" in result
        assert len(result["free_slots"]) == 2

    @pytest.mark.asyncio
    async def test_create_tentative_event(self, mock_client):
        result = await handle_tool_call(
            mock_client,
            "create_tentative_event",
            {
                "subject": "Proposed Meeting",
                "start_datetime": "2026-04-22T14:00:00",
                "end_datetime": "2026-04-22T14:30:00",
                "attendees": ["alice@example.com"],
            },
        )

        mock_client.create_tentative_event.assert_awaited_once()
        assert result["event_id"] == "evt_new"
        assert result["status"] == "created"
        assert result["show_as"] == "tentative"
        assert "outlook.office.com" in result["web_link"]

    @pytest.mark.asyncio
    async def test_update_event(self, mock_client):
        result = await handle_tool_call(
            mock_client,
            "update_event",
            {"event_id": "evt_new", "subject": "Updated Meeting"},
        )

        mock_client.update_event.assert_awaited_once()
        assert result["status"] == "updated"

    @pytest.mark.asyncio
    async def test_delete_event(self, mock_client):
        result = await handle_tool_call(mock_client, "delete_event", {"event_id": "evt_new"})

        mock_client.delete_event.assert_awaited_once_with("evt_new")
        assert result["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, mock_client):
        with pytest.raises(ValueError, match="Unknown tool"):
            await handle_tool_call(mock_client, "confirm_event", {})

    @pytest.mark.asyncio
    async def test_confirm_tool_does_not_exist(self, mock_client):
        """Attempting to call a confirm/book-like tool should raise ValueError."""
        for name in ["confirm", "confirm_event", "book_event", "accept_event"]:
            with pytest.raises(ValueError, match="Unknown tool"):
                await handle_tool_call(mock_client, name, {})


class TestEventFormatting:
    def test_summarize_events(self):
        events = [
            {
                "id": "e1",
                "subject": "Standup",
                "start": {"dateTime": "2026-04-21T09:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-04-21T09:30:00", "timeZone": "UTC"},
                "organizer": {"emailAddress": {"name": "Bram", "address": "bram@ex.com"}},
                "attendees": [{"emailAddress": {"address": "alice@ex.com"}}],
                "location": {"displayName": "Room B"},
                "showAs": "busy",
                "isAllDay": False,
            }
        ]
        result = _summarize_events(events)
        assert len(result) == 1
        assert result[0]["id"] == "e1"
        assert result[0]["subject"] == "Standup"
        assert "Bram" in result[0]["organizer"]
        assert result[0]["attendees"] == ["alice@ex.com"]
        assert result[0]["location"] == "Room B"
        assert result[0]["is_all_day"] is False

    def test_summarize_events_no_organizer(self):
        events = [{"id": "e1", "subject": "Solo"}]
        result = _summarize_events(events)
        assert result[0]["organizer"] == ""

    def test_format_event_full(self):
        event = {
            "id": "e1",
            "subject": "Meeting",
            "start": {"dateTime": "2026-04-21T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-04-21T10:00:00", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": "Bram", "address": "bram@ex.com"}},
            "attendees": [
                {"emailAddress": {"address": "alice@ex.com"}, "status": {"response": "accepted"}},
                {"emailAddress": {"address": "bob@ex.com"}, "status": {"response": "tentativelyAccepted"}},
            ],
            "location": {"displayName": "Room C"},
            "body": {"contentType": "HTML", "content": "<p>Agenda</p>"},
            "showAs": "tentative",
            "isAllDay": False,
            "isCancelled": False,
            "recurrence": None,
            "webLink": "https://outlook.office.com/calendar/item/e1",
        }
        result = _format_event(event)
        assert result["id"] == "e1"
        assert result["attendees"][0]["address"] == "alice@ex.com"
        assert result["attendees"][0]["response"] == "accepted"
        assert result["attendees"][1]["response"] == "tentativelyAccepted"
        assert result["location"] == "Room C"
        assert result["show_as"] == "tentative"
        assert result["web_link"] == "https://outlook.office.com/calendar/item/e1"
        assert "Bram" in result["organizer"]
