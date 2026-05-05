"""Tests for deep link URL generators."""

from __future__ import annotations

import pytest

from router.approvals.deep_links import (
    figma_file,
    get_deep_link,
    gmail_draft,
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
class TestGmailDraft:
    def test_basic_draft_id(self):
        url = gmail_draft("draft-abc")
        assert url == "https://mail.google.com/mail/u/0/#drafts/draft-abc"


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
    def test_outlook_target(self):
        url = get_deep_link("outlook", "draft123")
        assert url is not None
        assert "outlook.office.com" in url
        assert "draft123" in url

    def test_zoho_target(self):
        url = get_deep_link("zoho", "draft456")
        assert url is not None
        assert "zoho.com" in url

    def test_gmail_target(self):
        url = get_deep_link("gmail", "draft-x")
        assert url is not None
        assert "mail.google.com" in url

    def test_figma_target(self):
        url = get_deep_link("figma", "file789")
        assert url is not None
        assert "figma.com" in url

    def test_google_calendar_target(self):
        url = get_deep_link("google-calendar", "evt001")
        assert url is not None
        assert "calendar.google.com" in url

    def test_outlook_calendar_target(self):
        url = get_deep_link("outlook-calendar", "evt002")
        assert url is not None
        assert "outlook.office.com" in url

    def test_unknown_target_returns_none(self):
        assert get_deep_link("future-app", "id") is None
