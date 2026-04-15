"""Golden tests for Block Kit approval message rendering."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from router.approvals.block_kit import (
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_REQUEST_EDIT,
    build_approval_message,
    build_outcome_message,
)
from router.approvals.store import Draft


def _make_email_draft(**overrides) -> Draft:
    """Create a Draft for an email send approval."""
    defaults = {
        "draft_id": "draft-001",
        "agent_name": "lisa",
        "capability_type": "email",
        "capability_instance": "mine",
        "action_verb": "send",
        "payload": {
            "to": "recruiter@example.com",
            "subject": "Re: Engineering position",
            "body": "Thank you for reaching out. I'd love to discuss this opportunity.",
        },
        "slack_channel": "C12345",
        "slack_message_ts": "1705700000.000100",
    }
    defaults.update(overrides)
    return Draft(**defaults)


@pytest.mark.unit
class TestBuildApprovalMessage:
    def test_email_send_message_structure(self):
        draft = _make_email_draft()
        result = build_approval_message(draft, [ACTION_APPROVE_SEND, ACTION_REQUEST_EDIT, ACTION_DISCARD])

        blocks = result["blocks"]
        assert len(blocks) == 6  # header, context, divider, section, divider, actions

        # Header
        assert blocks[0]["type"] == "header"
        assert "Lisa wants to send" in blocks[0]["text"]["text"]

        # Context
        assert blocks[1]["type"] == "context"
        assert "mine" in blocks[1]["elements"][0]["text"]
        assert "email" in blocks[1]["elements"][0]["text"]

        # Content section
        assert blocks[3]["type"] == "section"
        content = blocks[3]["text"]["text"]
        assert "recruiter@example.com" in content
        assert "Engineering position" in content

        # Actions
        actions_block = blocks[5]
        assert actions_block["type"] == "actions"
        assert actions_block["block_id"] == "approval_draft-001"
        assert len(actions_block["elements"]) == 3

    def test_buttons_have_correct_action_ids(self):
        draft = _make_email_draft()
        result = build_approval_message(draft, [ACTION_APPROVE_SEND, ACTION_REQUEST_EDIT, ACTION_DISCARD])

        buttons = result["blocks"][5]["elements"]
        assert buttons[0]["action_id"] == ACTION_APPROVE_SEND
        assert buttons[1]["action_id"] == ACTION_REQUEST_EDIT
        assert buttons[2]["action_id"] == ACTION_DISCARD

    def test_buttons_carry_draft_id_as_value(self):
        draft = _make_email_draft(draft_id="my-draft-123")
        result = build_approval_message(draft, [ACTION_APPROVE_SEND, ACTION_DISCARD])

        for button in result["blocks"][5]["elements"]:
            assert button["value"] == "my-draft-123"

    def test_approve_button_is_primary_style(self):
        draft = _make_email_draft()
        result = build_approval_message(draft, [ACTION_APPROVE_SEND])

        button = result["blocks"][5]["elements"][0]
        assert button["style"] == "primary"
        assert button["text"]["text"] == "Send"

    def test_discard_button_is_danger_style(self):
        draft = _make_email_draft()
        result = build_approval_message(draft, [ACTION_DISCARD])

        button = result["blocks"][5]["elements"][0]
        assert button["style"] == "danger"
        assert button["text"]["text"] == "Discard"

    def test_edit_button_has_no_style(self):
        draft = _make_email_draft()
        result = build_approval_message(draft, [ACTION_REQUEST_EDIT])

        button = result["blocks"][5]["elements"][0]
        assert "style" not in button
        assert button["text"]["text"] == "Edit"

    def test_long_body_is_truncated(self):
        long_body = "A" * 500
        draft = _make_email_draft(payload={"body": long_body})
        result = build_approval_message(draft, [ACTION_APPROVE_SEND])

        content = result["blocks"][3]["text"]["text"]
        assert len(content) < 500
        assert "..." in content

    def test_empty_buttons_list(self):
        draft = _make_email_draft()
        result = build_approval_message(draft, [])

        # Should have 5 blocks (no actions block)
        assert len(result["blocks"]) == 5
        assert all(b["type"] != "actions" for b in result["blocks"])

    def test_calendar_draft_shows_attendees(self):
        draft = _make_email_draft(
            capability_type="calendar",
            action_verb="book",
            payload={
                "title": "Team standup",
                "attendees": ["alice@co.com", "bob@co.com"],
                "start_time": "2026-01-20 10:00 AM",
            },
        )
        result = build_approval_message(draft, [ACTION_APPROVE_SEND])

        content = result["blocks"][3]["text"]["text"]
        assert "Team standup" in content
        assert "alice@co.com" in content
        assert "bob@co.com" in content
        assert "2026-01-20 10:00 AM" in content


@pytest.mark.unit
class TestBuildOutcomeMessage:
    def test_approved_outcome(self):
        draft = _make_email_draft(
            status="approved",
            resolved_at=datetime(2026, 1, 15, 15, 42, 0, tzinfo=timezone.utc),
        )
        result = build_outcome_message(draft, approved=True)

        blocks = result["blocks"]
        assert len(blocks) == 4  # header, context, divider, section

        status_text = blocks[3]["text"]["text"]
        assert ":white_check_mark:" in status_text
        assert "Send" in status_text
        assert "03:42 PM" in status_text

    def test_discarded_outcome(self):
        draft = _make_email_draft(
            status="discarded",
            resolved_at=datetime(2026, 1, 15, 15, 42, 0, tzinfo=timezone.utc),
        )
        result = build_outcome_message(draft, approved=False)

        blocks = result["blocks"]
        status_text = blocks[3]["text"]["text"]
        assert ":x:" in status_text
        assert "Discarded" in status_text

    def test_outcome_has_no_action_buttons(self):
        draft = _make_email_draft(status="approved")
        result = build_outcome_message(draft, approved=True)

        for block in result["blocks"]:
            assert block["type"] != "actions"
