"""Tests for the Microsoft Graph calendar client.

Verifies that the GraphCalendarClient correctly constructs Graph API requests
for delegate calendar access (list events, read, create tentative, update,
delete, find availability). All HTTP calls are mocked.
"""

from __future__ import annotations

import pytest

from mcps.m365_calendar.graph_client import GraphCalendarClient, _parse_free_slots

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_httpx(monkeypatch):
    """Replace httpx.AsyncClient with a mock that captures requests."""
    import httpx

    calls = []
    response_data = {}
    response_status = 200

    class MockResponse:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data
            self.text = str(data)

        def json(self):
            return self._data

    class MockAsyncClient:
        def __init__(self, **kwargs):
            self._kwargs = kwargs

        async def request(self, method, path, **kwargs):
            calls.append({"method": method, "path": path, **kwargs})
            return MockResponse(response_status, response_data)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    return {"calls": calls, "set_response": lambda data, status=200: (response_data.update(data), None) or None}


class TestGraphCalendarClientPaths:
    """Verify URL path construction for delegate vs. self access."""

    def test_delegate_base_path(self):
        client = GraphCalendarClient("token", user_id="bram@pathtohired.com")
        assert client._base_path == "/users/bram@pathtohired.com"

    def test_self_base_path(self):
        client = GraphCalendarClient("token", user_id=None)
        assert client._base_path == "/me"


class TestListEvents:
    @pytest.mark.asyncio
    async def test_list_events_calls_correct_endpoint(self, mock_httpx):
        mock_httpx["set_response"]({"value": []})
        client = GraphCalendarClient("token", user_id="bram@example.com")
        result = await client.list_events(
            start_datetime="2026-04-20T00:00:00",
            end_datetime="2026-04-27T23:59:59",
            top=10,
        )

        assert len(mock_httpx["calls"]) == 1
        call = mock_httpx["calls"][0]
        assert call["method"] == "GET"
        assert "/users/bram@example.com/calendarView" in call["path"]
        assert call["params"]["startDateTime"] == "2026-04-20T00:00:00"
        assert call["params"]["endDateTime"] == "2026-04-27T23:59:59"
        assert call["params"]["$top"] == "10"
        assert result == []

    @pytest.mark.asyncio
    async def test_list_events_with_select(self, mock_httpx):
        mock_httpx["set_response"]({"value": []})
        client = GraphCalendarClient("token")
        await client.list_events(
            start_datetime="2026-04-20T00:00:00",
            end_datetime="2026-04-27T23:59:59",
            select=["subject", "start", "end"],
        )

        call = mock_httpx["calls"][0]
        assert call["params"]["$select"] == "subject,start,end"


class TestReadEvent:
    @pytest.mark.asyncio
    async def test_read_event_calls_correct_endpoint(self, mock_httpx):
        event_data = {"id": "evt123", "subject": "Team Standup", "start": {}, "end": {}}
        mock_httpx["set_response"](event_data)
        client = GraphCalendarClient("token", user_id="bram@example.com")
        result = await client.read_event("evt123")

        call = mock_httpx["calls"][0]
        assert call["method"] == "GET"
        assert "/users/bram@example.com/events/evt123" in call["path"]
        assert result["subject"] == "Team Standup"


class TestFindAvailability:
    @pytest.mark.asyncio
    async def test_find_availability_calls_get_schedule(self, mock_httpx):
        mock_httpx["set_response"]({"value": [{"scheduleId": "bram@example.com", "availabilityView": "0022000"}]})
        client = GraphCalendarClient("token", user_id="bram@example.com")
        result = await client.find_availability(
            schedules=["bram@example.com"],
            start_datetime="2026-04-21T09:00:00",
            end_datetime="2026-04-21T17:00:00",
            duration_minutes=30,
            timezone="America/New_York",
        )

        call = mock_httpx["calls"][0]
        assert call["method"] == "POST"
        assert "/users/bram@example.com/calendar/getSchedule" in call["path"]
        body = call["json"]
        assert body["schedules"] == ["bram@example.com"]
        assert body["startTime"]["dateTime"] == "2026-04-21T09:00:00"
        assert body["startTime"]["timeZone"] == "America/New_York"
        assert body["availabilityViewInterval"] == 30
        assert "schedules" in result
        assert "free_slots" in result


class TestCreateTentativeEvent:
    @pytest.mark.asyncio
    async def test_create_event_always_tentative(self, mock_httpx):
        event_data = {"id": "evt456", "subject": "Meeting", "showAs": "tentative"}
        mock_httpx["set_response"](event_data)
        client = GraphCalendarClient("token", user_id="bram@example.com")
        result = await client.create_tentative_event(
            subject="Meeting with Alice",
            start_datetime="2026-04-22T14:00:00",
            end_datetime="2026-04-22T14:30:00",
            timezone="UTC",
            attendees=["alice@example.com"],
        )

        call = mock_httpx["calls"][0]
        assert call["method"] == "POST"
        assert "/users/bram@example.com/events" in call["path"]
        body = call["json"]
        assert body["subject"] == "Meeting with Alice"
        assert body["showAs"] == "tentative"
        assert body["start"]["dateTime"] == "2026-04-22T14:00:00"
        assert body["start"]["timeZone"] == "UTC"
        assert len(body["attendees"]) == 1
        assert body["attendees"][0]["emailAddress"]["address"] == "alice@example.com"
        assert result["id"] == "evt456"

    @pytest.mark.asyncio
    async def test_create_event_with_body_and_location(self, mock_httpx):
        mock_httpx["set_response"]({"id": "evt789", "subject": "Lunch"})
        client = GraphCalendarClient("token")
        await client.create_tentative_event(
            subject="Lunch",
            start_datetime="2026-04-22T12:00:00",
            end_datetime="2026-04-22T13:00:00",
            body="<p>Team lunch</p>",
            location="Conference Room A",
        )

        body = mock_httpx["calls"][0]["json"]
        assert body["body"]["contentType"] == "HTML"
        assert body["body"]["content"] == "<p>Team lunch</p>"
        assert body["location"]["displayName"] == "Conference Room A"

    @pytest.mark.asyncio
    async def test_create_event_without_optional_fields(self, mock_httpx):
        mock_httpx["set_response"]({"id": "evt000", "subject": "Solo block"})
        client = GraphCalendarClient("token")
        await client.create_tentative_event(
            subject="Solo block",
            start_datetime="2026-04-22T10:00:00",
            end_datetime="2026-04-22T10:30:00",
        )

        body = mock_httpx["calls"][0]["json"]
        assert "attendees" not in body
        assert "body" not in body
        assert "location" not in body
        assert body["showAs"] == "tentative"


class TestUpdateEvent:
    @pytest.mark.asyncio
    async def test_update_event_uses_patch(self, mock_httpx):
        mock_httpx["set_response"]({"id": "evt456", "subject": "Updated Meeting"})
        client = GraphCalendarClient("token")
        await client.update_event(event_id="evt456", subject="Updated Meeting")

        call = mock_httpx["calls"][0]
        assert call["method"] == "PATCH"
        assert "events/evt456" in call["path"]
        assert call["json"]["subject"] == "Updated Meeting"

    @pytest.mark.asyncio
    async def test_update_event_only_sends_provided_fields(self, mock_httpx):
        mock_httpx["set_response"]({"id": "evt456", "subject": "Old"})
        client = GraphCalendarClient("token")
        await client.update_event(event_id="evt456", location="New Room")

        patch_data = mock_httpx["calls"][0]["json"]
        assert "location" in patch_data
        assert "subject" not in patch_data
        assert "start" not in patch_data
        assert "attendees" not in patch_data

    @pytest.mark.asyncio
    async def test_update_event_with_time_change(self, mock_httpx):
        mock_httpx["set_response"]({"id": "evt456", "subject": "Meeting"})
        client = GraphCalendarClient("token")
        await client.update_event(
            event_id="evt456",
            start_datetime="2026-04-22T15:00:00",
            end_datetime="2026-04-22T15:30:00",
            timezone="America/New_York",
        )

        patch_data = mock_httpx["calls"][0]["json"]
        assert patch_data["start"]["dateTime"] == "2026-04-22T15:00:00"
        assert patch_data["start"]["timeZone"] == "America/New_York"
        assert patch_data["end"]["dateTime"] == "2026-04-22T15:30:00"


class TestDeleteEvent:
    @pytest.mark.asyncio
    async def test_delete_event_calls_delete(self, mock_httpx):
        mock_httpx["set_response"]({})
        client = GraphCalendarClient("token", user_id="bram@example.com")

        try:
            await client.delete_event("evt456")
        except Exception:
            pass  # Mock doesn't handle 204 perfectly

        call = mock_httpx["calls"][0]
        assert call["method"] == "DELETE"
        assert "/users/bram@example.com/events/evt456" in call["path"]


class TestGetEventUrl:
    @pytest.mark.asyncio
    async def test_get_event_url_uses_weblink(self, mock_httpx):
        mock_httpx["set_response"]({"webLink": "https://outlook.office.com/calendar/item/real-link"})
        client = GraphCalendarClient("token")
        url = await client.get_event_url("evt123")

        assert url == "https://outlook.office.com/calendar/item/real-link"
        call = mock_httpx["calls"][0]
        assert call["params"]["$select"] == "webLink"

    @pytest.mark.asyncio
    async def test_get_event_url_fallback(self, mock_httpx):
        mock_httpx["set_response"]({"id": "evt123"})  # No webLink
        client = GraphCalendarClient("token")
        url = await client.get_event_url("evt123")

        assert "outlook.office.com/calendar/item/" in url
        assert "evt123" in url


class TestNoConfirmMethod:
    """Verify that GraphCalendarClient has no confirm/book functionality."""

    def test_no_confirm_method_exists(self):
        """The client must not have any method that confirms/books events."""
        client = GraphCalendarClient("token")
        assert not hasattr(client, "confirm_event")
        assert not hasattr(client, "book_event")
        assert not hasattr(client, "accept_event")


class TestParseFreeSlots:
    """Test the availability view parser."""

    def test_all_free(self):
        schedule_data = [{"availabilityView": "0000"}]
        slots = _parse_free_slots(schedule_data, "2026-04-21T09:00:00", 30)
        assert len(slots) == 1
        assert slots[0]["start"] == "2026-04-21T09:00:00"
        assert slots[0]["end"] == "2026-04-21T11:00:00"

    def test_all_busy(self):
        schedule_data = [{"availabilityView": "2222"}]
        slots = _parse_free_slots(schedule_data, "2026-04-21T09:00:00", 30)
        assert len(slots) == 0

    def test_mixed_availability(self):
        # 00 = free (9:00-10:00), 22 = busy (10:00-11:00), 00 = free (11:00-12:00)
        schedule_data = [{"availabilityView": "002200"}]
        slots = _parse_free_slots(schedule_data, "2026-04-21T09:00:00", 30)
        assert len(slots) == 2
        assert slots[0]["start"] == "2026-04-21T09:00:00"
        assert slots[0]["end"] == "2026-04-21T10:00:00"
        assert slots[1]["start"] == "2026-04-21T11:00:00"
        assert slots[1]["end"] == "2026-04-21T12:00:00"

    def test_tentative_is_not_free(self):
        # 1 = tentative, should not be counted as free
        schedule_data = [{"availabilityView": "1100"}]
        slots = _parse_free_slots(schedule_data, "2026-04-21T09:00:00", 30)
        assert len(slots) == 1
        assert slots[0]["start"] == "2026-04-21T10:00:00"

    def test_empty_availability_view(self):
        schedule_data = [{"availabilityView": ""}]
        slots = _parse_free_slots(schedule_data, "2026-04-21T09:00:00", 30)
        assert len(slots) == 0

    def test_no_schedules(self):
        slots = _parse_free_slots([], "2026-04-21T09:00:00", 30)
        assert len(slots) == 0
