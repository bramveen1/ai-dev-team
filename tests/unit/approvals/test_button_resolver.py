"""Tests for the permission-aware button resolver.

Covers full-send, draft-only, and propose-only permission patterns
across email, social, and calendar capability types.
"""

from __future__ import annotations

import pytest

from capabilities.models import CapabilityInstance
from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_OPEN_IN_APP,
    ACTION_REQUEST_EDIT,
)
from router.approvals.button_resolver import ButtonSpec, resolve_buttons


def _make_instance(provider: str, permissions: list[str], **kwargs) -> CapabilityInstance:
    """Create a CapabilityInstance with given permissions."""
    defaults = {
        "instance": "test",
        "provider": provider,
        "account": "test@example.com",
        "ownership": "self",
        "permissions": permissions,
    }
    defaults.update(kwargs)
    return CapabilityInstance(**defaults)


@pytest.mark.unit
class TestResolveButtonsFullPermission:
    """Tests for when the agent has direct action permission."""

    def test_email_with_send_permission(self):
        instance = _make_instance("zoho-mcp", ["read", "send", "draft-create"])
        buttons = resolve_buttons("email", instance, "send")

        assert len(buttons) == 3
        assert buttons[0].action_id == ACTION_APPROVE_SEND
        assert buttons[0].text == "Send"
        assert buttons[0].style == "primary"
        assert buttons[1].action_id == ACTION_REQUEST_EDIT
        assert buttons[1].text == "Edit"
        assert buttons[2].action_id == ACTION_DISCARD
        assert buttons[2].style == "danger"

    def test_social_with_publish_permission(self):
        instance = _make_instance("twitter-mcp", ["read", "publish"])
        buttons = resolve_buttons("social", instance, "publish")

        assert buttons[0].action_id == ACTION_APPROVE_PUBLISH
        assert buttons[0].text == "Publish"
        assert buttons[0].style == "primary"

    def test_calendar_with_book_permission(self):
        instance = _make_instance("m365-mcp", ["read", "book"])
        buttons = resolve_buttons("calendar", instance, "book")

        assert buttons[0].action_id == ACTION_APPROVE_BOOK
        assert buttons[0].text == "Book"
        assert buttons[0].style == "primary"

    def test_full_permission_has_no_url(self):
        instance = _make_instance("zoho-mcp", ["send"])
        buttons = resolve_buttons("email", instance, "send")

        for button in buttons:
            assert button.url is None


@pytest.mark.unit
class TestResolveButtonsDraftOnly:
    """Tests for when the agent lacks direct action permission (draft-only)."""

    def test_email_without_send_shows_open_in_outlook(self):
        instance = _make_instance("m365-mcp", ["read", "draft-create", "draft-update"])
        buttons = resolve_buttons("email", instance, "send")

        assert len(buttons) == 3
        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Outlook" in buttons[0].text
        assert buttons[0].style == "primary"
        assert buttons[1].action_id == ACTION_REQUEST_EDIT
        assert buttons[1].text == "Redraft"
        assert buttons[2].action_id == ACTION_DISCARD

    def test_email_without_send_shows_open_in_zoho(self):
        instance = _make_instance("zoho-mcp", ["read", "draft-create"])
        buttons = resolve_buttons("email", instance, "send")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Zoho" in buttons[0].text

    def test_social_without_publish_shows_open_in_buffer(self):
        instance = _make_instance("buffer-mcp", ["read", "draft"])
        buttons = resolve_buttons("social", instance, "publish")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Buffer" in buttons[0].text

    def test_calendar_without_book_shows_fallback(self):
        instance = _make_instance("m365-mcp", ["read", "propose"])
        buttons = resolve_buttons("calendar", instance, "book")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP

    def test_deep_link_url_is_passed_through(self):
        instance = _make_instance("m365-mcp", ["read", "draft-create"])
        deep_link = "https://outlook.office.com/mail/drafts/id/abc123"
        buttons = resolve_buttons("email", instance, "send", deep_link_url=deep_link)

        assert buttons[0].url == deep_link

    def test_no_deep_link_url_when_not_provided(self):
        instance = _make_instance("m365-mcp", ["read", "draft-create"])
        buttons = resolve_buttons("email", instance, "send")

        assert buttons[0].url is None


@pytest.mark.unit
class TestResolveButtonsEdgeCases:
    def test_unknown_provider_uses_provider_name(self):
        instance = _make_instance("custom-mcp", ["read"])
        buttons = resolve_buttons("email", instance, "send")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "custom-mcp" in buttons[0].text

    def test_discard_always_present(self):
        for perms in [["send"], ["read"]]:
            instance = _make_instance("zoho-mcp", perms)
            buttons = resolve_buttons("email", instance, "send")
            assert any(b.action_id == ACTION_DISCARD for b in buttons)

    def test_button_spec_is_dataclass(self):
        spec = ButtonSpec(action_id="test", text="Test", style="primary", url="https://example.com")
        assert spec.action_id == "test"
        assert spec.text == "Test"
        assert spec.style == "primary"
        assert spec.url == "https://example.com"


@pytest.mark.unit
class TestZohoVsM365EmailFlows:
    """Verify Lisa's two email flows render correctly."""

    def test_zoho_full_send_flow(self):
        """Lisa's own Zoho inbox — full send permission."""
        instance = _make_instance(
            "zoho-mcp",
            ["read", "send", "archive", "draft-create", "draft-update", "draft-delete"],
            instance="mine",
            account="lisa@pathtohired.com",
            ownership="self",
        )
        buttons = resolve_buttons("email", instance, "send")

        assert buttons[0].action_id == ACTION_APPROVE_SEND
        assert buttons[0].text == "Send"
        assert buttons[1].action_id == ACTION_REQUEST_EDIT
        assert buttons[1].text == "Edit"
        assert buttons[2].action_id == ACTION_DISCARD

    def test_m365_draft_only_flow(self):
        """Lisa's delegated M365 inbox — draft-only, no send."""
        instance = _make_instance(
            "m365-mcp",
            ["read", "draft-create", "draft-update", "draft-delete"],
            instance="bram",
            account="bram@pathtohired.com",
            ownership="delegate",
        )
        buttons = resolve_buttons("email", instance, "send")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Outlook" in buttons[0].text
        assert buttons[1].action_id == ACTION_REQUEST_EDIT
        assert buttons[1].text == "Redraft"
        assert buttons[2].action_id == ACTION_DISCARD
