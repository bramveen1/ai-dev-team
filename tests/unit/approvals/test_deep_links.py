"""Tests for deep link URL generators."""

from __future__ import annotations

import pytest

from router.approvals.deep_links import (
    figma_file,
    get_deep_link,
    google_calendar_event,
    outlook_calendar_event,
    outlook_draft,
    zoho_draft,
)


@pytest.mark.unit
class TestOutlookDraft:
    def test_basic_draft_id(self):
        url = outlook_draft("AAMkAGI2TG93AAA=")
        assert url == "https://outlook.office.com/mail/drafts/id/AAMkAGI2TG93AAA%3D"

    def test_url_encodes_special_chars(self):
        url = outlook_draft("draft/with spaces&special")
        assert "draft%2Fwith%20spaces%26special" in url


@pytest.mark.unit
class TestZohoDraft:
    def test_basic_draft_id(self):
        url = zoho_draft("12345678")
        assert url == "https://mail.zoho.com/zm/#compose/12345678"

    def test_url_encodes_special_chars(self):
        url = zoho_draft("id with/slash")
        assert "id%20with%2Fslash" in url


@pytest.mark.unit
class TestFigmaFile:
    def test_basic_file_id(self):
        url = figma_file("AbCdEfG12345")
        assert url == "https://www.figma.com/file/AbCdEfG12345"


@pytest.mark.unit
class TestGoogleCalendarEvent:
    def test_basic_event_id(self):
        url = google_calendar_event("event123")
        assert url == "https://calendar.google.com/calendar/event?eid=event123"


@pytest.mark.unit
class TestOutlookCalendarEvent:
    def test_basic_event_id(self):
        url = outlook_calendar_event("AAMkAGI2=")
        assert url == "https://outlook.office.com/calendar/item/AAMkAGI2%3D"


@pytest.mark.unit
class TestGetDeepLink:
    def test_email_m365(self):
        url = get_deep_link("email", "m365-mcp", "draft123")
        assert url is not None
        assert "outlook.office.com" in url
        assert "draft123" in url

    def test_email_zoho(self):
        url = get_deep_link("email", "zoho-mcp", "draft456")
        assert url is not None
        assert "zoho.com" in url

    def test_design_figma(self):
        url = get_deep_link("design", "figma-mcp", "file789")
        assert url is not None
        assert "figma.com" in url

    def test_calendar_google(self):
        url = get_deep_link("calendar", "google-calendar-mcp", "evt001")
        assert url is not None
        assert "calendar.google.com" in url

    def test_calendar_m365(self):
        url = get_deep_link("calendar", "m365-mcp", "evt002")
        assert url is not None
        assert "outlook.office.com" in url

    def test_unknown_provider_returns_none(self):
        assert get_deep_link("email", "unknown-mcp", "id") is None

    def test_unknown_capability_returns_none(self):
        assert get_deep_link("unknown", "m365-mcp", "id") is None
