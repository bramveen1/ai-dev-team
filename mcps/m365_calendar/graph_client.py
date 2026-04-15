"""Microsoft Graph API client for delegate calendar access.

Wraps the Graph v1.0 REST API for reading events, checking availability,
and managing tentative calendar entries in a shared/delegated calendar.
Uses access tokens obtained via OAuth2 device-code or refresh-token flow.

This client intentionally omits any confirm/book functionality — the trust
boundary is enforced here at the API client level. Events are always
created with showAs="tentative".
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphCalendarError(Exception):
    """Raised when a Graph API call fails."""

    def __init__(self, status_code: int, message: str, error_code: str | None = None):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Graph API error {status_code}: {message}")


class GraphCalendarClient:
    """Delegate-access calendar client for Microsoft Graph.

    Args:
        access_token: OAuth2 access token with Calendars.Read.Shared and
                      Calendars.ReadWrite.Shared scopes.
        user_id: The calendar owner's UPN or object ID (e.g. bram@pathtohired.com).
                 When provided, uses /users/{user_id}/... endpoints for delegate access.
                 When None, uses /me/... endpoints.
    """

    def __init__(self, access_token: str, user_id: str | None = None) -> None:
        self._token = access_token
        self._user_id = user_id
        self._client = httpx.AsyncClient(
            base_url=GRAPH_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    @property
    def _base_path(self) -> str:
        """Return the base path for calendar operations."""
        if self._user_id:
            return f"/users/{quote(self._user_id, safe='@')}"
        return "/me"

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make an authenticated request to the Graph API."""
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                error_body = response.json()
                error_msg = error_body.get("error", {}).get("message", response.text)
                error_code = error_body.get("error", {}).get("code")
            except (json.JSONDecodeError, KeyError):
                error_msg = response.text
                error_code = None
            raise GraphCalendarError(response.status_code, error_msg, error_code)
        if response.status_code == 204:
            return {}
        return response.json()

    async def list_events(
        self,
        start_datetime: str,
        end_datetime: str,
        top: int = 25,
        select: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List calendar events in a date/time range using calendarView.

        Args:
            start_datetime: ISO 8601 start datetime (e.g. "2026-04-20T00:00:00").
            end_datetime: ISO 8601 end datetime (e.g. "2026-04-27T23:59:59").
            top: Maximum number of events to return.
            select: List of fields to include.

        Returns:
            List of event objects.
        """
        params: dict[str, str] = {
            "startDateTime": start_datetime,
            "endDateTime": end_datetime,
            "$top": str(top),
            "$orderby": "start/dateTime",
        }
        if select:
            params["$select"] = ",".join(select)

        result = await self._request(
            "GET",
            f"{self._base_path}/calendarView",
            params=params,
        )
        return result.get("value", [])

    async def read_event(self, event_id: str) -> dict[str, Any]:
        """Read a single calendar event by ID.

        Args:
            event_id: The Graph event ID.

        Returns:
            Full event object.
        """
        return await self._request("GET", f"{self._base_path}/events/{event_id}")

    async def find_availability(
        self,
        schedules: list[str],
        start_datetime: str,
        end_datetime: str,
        duration_minutes: int = 30,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Find free/busy information and available time slots.

        Uses the Graph getSchedule API to retrieve availability, then
        computes free slots of the requested duration.

        Args:
            schedules: List of email addresses to check availability for.
            start_datetime: ISO 8601 start datetime.
            end_datetime: ISO 8601 end datetime.
            duration_minutes: Minimum slot duration in minutes.
            timezone: IANA timezone name (e.g. "America/New_York").

        Returns:
            Dict with 'schedules' (raw schedule data) and 'free_slots'
            (computed available windows).
        """
        body = {
            "schedules": schedules,
            "startTime": {"dateTime": start_datetime, "timeZone": timezone},
            "endTime": {"dateTime": end_datetime, "timeZone": timezone},
            "availabilityViewInterval": duration_minutes,
        }

        result = await self._request(
            "POST",
            f"{self._base_path}/calendar/getSchedule",
            json=body,
        )

        schedule_data = result.get("value", [])
        free_slots = _parse_free_slots(
            schedule_data,
            start_datetime,
            duration_minutes,
        )

        return {
            "schedules": schedule_data,
            "free_slots": free_slots,
        }

    async def create_tentative_event(
        self,
        subject: str,
        start_datetime: str,
        end_datetime: str,
        timezone: str = "UTC",
        attendees: list[str] | None = None,
        body: str | None = None,
        location: str | None = None,
        body_content_type: str = "HTML",
    ) -> dict[str, Any]:
        """Create a new tentative calendar event.

        Events are always created with showAs="tentative" — the organizer
        must confirm in Outlook. This is the propose-only trust boundary.

        Args:
            subject: Event subject/title.
            start_datetime: ISO 8601 start datetime.
            end_datetime: ISO 8601 end datetime.
            timezone: IANA timezone name.
            attendees: List of attendee email addresses.
            body: Event body/description content.
            location: Event location string.
            body_content_type: Content type of the body ("HTML" or "Text").

        Returns:
            The created event object (includes the event ID).
        """
        event_data: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": start_datetime, "timeZone": timezone},
            "end": {"dateTime": end_datetime, "timeZone": timezone},
            "showAs": "tentative",
        }

        if attendees:
            event_data["attendees"] = [{"emailAddress": {"address": addr}, "type": "required"} for addr in attendees]

        if body:
            event_data["body"] = {"contentType": body_content_type, "content": body}

        if location:
            event_data["location"] = {"displayName": location}

        return await self._request("POST", f"{self._base_path}/events", json=event_data)

    async def update_event(
        self,
        event_id: str,
        subject: str | None = None,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
        timezone: str | None = None,
        attendees: list[str] | None = None,
        body: str | None = None,
        location: str | None = None,
        body_content_type: str = "HTML",
    ) -> dict[str, Any]:
        """Update an existing calendar event.

        Only provided fields are updated; None fields are left unchanged.

        Args:
            event_id: The Graph event ID.
            subject: New subject (or None to leave unchanged).
            start_datetime: New start datetime (or None).
            end_datetime: New end datetime (or None).
            timezone: Timezone for start/end (required if start/end provided).
            attendees: New attendee list (or None).
            body: New body content (or None).
            location: New location (or None).
            body_content_type: Content type of the body.

        Returns:
            The updated event object.
        """
        patch_data: dict[str, Any] = {}

        if subject is not None:
            patch_data["subject"] = subject
        if start_datetime is not None:
            tz = timezone or "UTC"
            patch_data["start"] = {"dateTime": start_datetime, "timeZone": tz}
        if end_datetime is not None:
            tz = timezone or "UTC"
            patch_data["end"] = {"dateTime": end_datetime, "timeZone": tz}
        if attendees is not None:
            patch_data["attendees"] = [{"emailAddress": {"address": addr}, "type": "required"} for addr in attendees]
        if body is not None:
            patch_data["body"] = {"contentType": body_content_type, "content": body}
        if location is not None:
            patch_data["location"] = {"displayName": location}

        return await self._request("PATCH", f"{self._base_path}/events/{event_id}", json=patch_data)

    async def delete_event(self, event_id: str) -> None:
        """Delete a calendar event.

        Args:
            event_id: The Graph event ID of the event to delete.
        """
        await self._request("DELETE", f"{self._base_path}/events/{event_id}")

    async def get_event_url(self, event_id: str) -> str:
        """Generate an Outlook deep link URL for a calendar event.

        Returns a URL that opens the event in Outlook web app for manual
        review/confirmation.

        Args:
            event_id: The Graph event ID.

        Returns:
            Outlook web app URL to open the event.
        """
        try:
            event = await self._request(
                "GET",
                f"{self._base_path}/events/{event_id}",
                params={"$select": "webLink"},
            )
            if "webLink" in event:
                return event["webLink"]
        except GraphCalendarError:
            logger.warning("Could not fetch webLink for event %s, using fallback URL", event_id)

        return f"https://outlook.office.com/calendar/item/{quote(event_id, safe='')}"

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


def _parse_free_slots(
    schedule_data: list[dict[str, Any]],
    start_datetime: str,
    interval_minutes: int,
) -> list[dict[str, str]]:
    """Parse getSchedule response to find free time slots.

    Analyzes the availabilityView string from the Graph API response.
    Each character represents one interval:
      0 = free, 1 = tentative, 2 = busy, 3 = out of office, 4 = working elsewhere

    Only '0' (free) is considered available.

    Args:
        schedule_data: The 'value' array from getSchedule response.
        start_datetime: ISO 8601 start datetime of the query window.
        interval_minutes: Duration of each availability interval in minutes.

    Returns:
        List of {start, end} dicts for consecutive free windows.
    """
    from datetime import datetime, timedelta

    free_slots: list[dict[str, str]] = []

    for schedule in schedule_data:
        availability_view = schedule.get("availabilityView", "")
        if not availability_view:
            continue

        try:
            base_time = datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
        except ValueError:
            base_time = datetime.fromisoformat(start_datetime)

        interval = timedelta(minutes=interval_minutes)

        slot_start: datetime | None = None
        for i, char in enumerate(availability_view):
            current_time = base_time + (interval * i)
            if char == "0":
                if slot_start is None:
                    slot_start = current_time
            else:
                if slot_start is not None:
                    slot_end = current_time
                    free_slots.append(
                        {
                            "start": slot_start.isoformat(),
                            "end": slot_end.isoformat(),
                        }
                    )
                    slot_start = None

        # Handle trailing free slot
        if slot_start is not None:
            slot_end = base_time + (interval * len(availability_view))
            free_slots.append(
                {
                    "start": slot_start.isoformat(),
                    "end": slot_end.isoformat(),
                }
            )

    return free_slots
