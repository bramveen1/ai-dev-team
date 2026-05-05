"""Tests for the pack-aware button resolver.

Three rendering paths:
- Pack-backed + verb in ``approve``: direct action button.
- Pack-backed + verb NOT in ``approve``: discard-only fallback.
- Connector-backed (``target`` set, no pack): "Open in <App>" deep-link.
- Neither pack nor target: discard-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_OPEN_IN_APP,
    ACTION_REQUEST_EDIT,
)
from router.approvals.button_resolver import ButtonSpec, get_app_name, resolve_buttons
from router.packs.loader import Pack


def _make_pack(name: str, *, approve: list[str] | None = None) -> Pack:
    """Build a minimal Pack — only ``name`` and ``approve`` matter here."""
    return Pack(name=name, path=Path("/tmp/" + name), approve=approve or [])


# ---------------------------------------------------------------------------
# Pack-backed: verb in approve list → direct action
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPackBackedDirectAction:
    def test_send_in_approve_renders_send_button(self):
        pack = _make_pack("zoho-mail", approve=["send"])
        buttons = resolve_buttons(action_verb="send", pack=pack)

        assert len(buttons) == 3
        assert buttons[0].action_id == ACTION_APPROVE_SEND
        assert buttons[0].text == "Send"
        assert buttons[0].style == "primary"
        assert buttons[1].action_id == ACTION_REQUEST_EDIT
        assert buttons[1].text == "Edit"
        assert buttons[2].action_id == ACTION_DISCARD
        assert buttons[2].style == "danger"

    def test_publish_in_approve_renders_publish_button(self):
        pack = _make_pack("buffer", approve=["publish"])
        buttons = resolve_buttons(action_verb="publish", pack=pack)

        assert buttons[0].action_id == ACTION_APPROVE_PUBLISH
        assert buttons[0].text == "Publish"
        assert buttons[0].style == "primary"

    def test_book_in_approve_renders_book_button(self):
        pack = _make_pack("calendar-pro", approve=["book"])
        buttons = resolve_buttons(action_verb="book", pack=pack)

        assert buttons[0].action_id == ACTION_APPROVE_BOOK
        assert buttons[0].text == "Book"
        assert buttons[0].style == "primary"

    def test_merge_reuses_send_action_id(self):
        """Pack verbs without a dedicated action_id (e.g. merge) reuse APPROVE_SEND."""
        pack = _make_pack("github", approve=["merge"])
        buttons = resolve_buttons(action_verb="merge", pack=pack)

        assert buttons[0].action_id == ACTION_APPROVE_SEND
        assert buttons[0].text == "Merge"
        assert buttons[0].style == "primary"

    def test_direct_action_buttons_have_no_url(self):
        pack = _make_pack("zoho-mail", approve=["send"])
        buttons = resolve_buttons(action_verb="send", pack=pack)

        for button in buttons:
            assert button.url is None


# ---------------------------------------------------------------------------
# Pack-backed: verb NOT in approve list → discard-only safety net
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPackBackedNotApproveGated:
    def test_verb_not_in_approve_falls_back_to_discard_only(self):
        """An agent shouldn't draft a verb it's allowed to do directly,
        but if it does we don't render a misleading approval button."""
        pack = _make_pack("zoho-mail", approve=[])
        buttons = resolve_buttons(action_verb="send", pack=pack)

        assert len(buttons) == 1
        assert buttons[0].action_id == ACTION_DISCARD

    def test_partial_approve_list_still_gates_correctly(self):
        pack = _make_pack("github", approve=["merge"])

        merge_buttons = resolve_buttons(action_verb="merge", pack=pack)
        assert merge_buttons[0].action_id == ACTION_APPROVE_SEND

        comment_buttons = resolve_buttons(action_verb="comment", pack=pack)
        assert comment_buttons == [ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger")]


# ---------------------------------------------------------------------------
# Connector-backed: target set, no pack → deep-link path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConnectorBackedDeepLink:
    def test_outlook_target_renders_open_in_outlook(self):
        url = "https://outlook.office.com/mail/drafts/id/abc"
        buttons = resolve_buttons(action_verb="send", target="outlook", deep_link_url=url)

        assert len(buttons) == 3
        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Outlook" in buttons[0].text
        assert buttons[0].style == "primary"
        assert buttons[0].url == url
        assert buttons[1].action_id == ACTION_REQUEST_EDIT
        assert buttons[1].text == "Redraft"
        assert buttons[2].action_id == ACTION_DISCARD

    def test_gmail_target_renders_open_in_gmail(self):
        buttons = resolve_buttons(action_verb="send", target="gmail")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Gmail" in buttons[0].text

    def test_outlook_calendar_target(self):
        buttons = resolve_buttons(action_verb="book", target="outlook-calendar")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Outlook Calendar" in buttons[0].text

    def test_unknown_target_uses_target_string_as_app_name(self):
        buttons = resolve_buttons(action_verb="send", target="some-future-thing")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "some-future-thing" in buttons[0].text

    def test_no_deep_link_url_renders_button_without_url(self):
        buttons = resolve_buttons(action_verb="send", target="outlook")

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert buttons[0].url is None


# ---------------------------------------------------------------------------
# Legacy / fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoPackNoTarget:
    """Legacy drafts (capability_type/capability_instance only) reach the
    resolver as pack=None, target=None. We render discard-only — the
    safest thing when we can't tell what the agent meant."""

    def test_neither_pack_nor_target_renders_discard_only(self):
        buttons = resolve_buttons(action_verb="send")

        assert buttons == [ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger")]

    def test_pack_wins_over_target_when_both_provided(self):
        """Defensive: if both arrive (shouldn't, but just in case),
        treat the draft as pack-backed."""
        pack = _make_pack("github", approve=["merge"])
        buttons = resolve_buttons(action_verb="merge", pack=pack, target="outlook")

        assert buttons[0].action_id == ACTION_APPROVE_SEND
        assert buttons[0].text == "Merge"


# ---------------------------------------------------------------------------
# get_app_name helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetAppName:
    def test_known_target_returns_pretty_name(self):
        assert get_app_name("outlook") == "Outlook"
        assert get_app_name("gmail") == "Gmail"
        assert get_app_name("google-calendar") == "Google Calendar"

    def test_unknown_target_returns_target_unchanged(self):
        assert get_app_name("some-mystery-app") == "some-mystery-app"


# ---------------------------------------------------------------------------
# Lisa's two flows
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLisaTwoFlows:
    """Verify Lisa's email flows render correctly under the new schema."""

    def test_zoho_self_send_flow_via_pack(self):
        """zoho-mail has approve:[] in production — Lisa autonomous-sends.
        The agent shouldn't draft, but if she does, render discard-only."""
        pack = _make_pack("zoho-mail", approve=[])
        buttons = resolve_buttons(action_verb="send", pack=pack)

        assert buttons[0].action_id == ACTION_DISCARD

    def test_m365_delegate_send_via_outlook_connector(self):
        """Bram's M365 inbox → connector path → Open in Outlook."""
        url = "https://outlook.office.com/mail/drafts/id/AAMkAGI"
        buttons = resolve_buttons(action_verb="send", target="outlook", deep_link_url=url)

        assert buttons[0].action_id == ACTION_OPEN_IN_APP
        assert "Outlook" in buttons[0].text
        assert buttons[0].url == url
