"""Tests for the approval adapter layer.

Covers:
- SlackApprovalAdapter.render_approval_card: parity with build_approval_message_from_specs
- Rendering for approve and discard outcomes
- Expired/stale draft cards
- E2E callback path: render card → on_approval → verify state + callback
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from router.approvals.adapters.slack import SlackApprovalAdapter
from router.approvals.block_kit import (
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_REQUEST_EDIT,
    build_approval_message_from_specs,
)
from router.approvals.button_resolver import ButtonSpec
from router.approvals.card import ApprovalCard
from router.approvals.core import on_approval
from router.approvals.store import Draft, DraftStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_draft(**overrides) -> Draft:
    defaults = {
        "draft_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "capability_type": "email",
        "capability_instance": "mine",
        "action_verb": "send",
        "payload": {
            "to": "recruiter@example.com",
            "subject": "Re: Engineering position",
            "body": "Thank you for reaching out.",
        },
        "slack_channel": "C12345",
        "slack_message_ts": "1705700000.000100",
        "status": "pending",
        "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Draft(**defaults)


def _make_card(draft: Draft, actions: list | None = None) -> ApprovalCard:
    if actions is None:
        actions = [
            ButtonSpec(action_id=ACTION_APPROVE_SEND, text="Send", style="primary"),
            ButtonSpec(action_id=ACTION_REQUEST_EDIT, text="Edit"),
            ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
        ]
    return ApprovalCard(
        draft_id=draft.draft_id,
        agent_name=draft.agent_name,
        pack=None,
        capability_type=draft.capability_type,
        capability_instance=draft.capability_instance,
        action_verb=draft.action_verb,
        summary="",
        payload=draft.payload,
        actions=actions,
        expires_at=draft.expires_at,
    )


@pytest.fixture
def adapter() -> SlackApprovalAdapter:
    return SlackApprovalAdapter()


@pytest.fixture
def store(tmp_path) -> DraftStore:
    db_path = str(tmp_path / "test_adapters.db")
    s = DraftStore(db_path)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Parity test — byte-identical to build_approval_message_from_specs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackAdapterParity:
    def test_render_approval_card_byte_identical_to_block_kit(self, adapter):
        """render_approval_card must produce exactly the same payload as
        build_approval_message_from_specs — no structural delta whatsoever."""
        draft = _make_draft()
        specs = [
            ButtonSpec(action_id=ACTION_APPROVE_SEND, text="Send", style="primary"),
            ButtonSpec(action_id=ACTION_REQUEST_EDIT, text="Edit"),
            ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
        ]
        card = _make_card(draft, specs)

        result = adapter.render_approval_card(card)
        expected = build_approval_message_from_specs(draft, specs)

        assert result == expected, "render_approval_card output diverges from build_approval_message_from_specs"

    def test_parity_with_empty_actions(self, adapter):
        draft = _make_draft()
        card = _make_card(draft, [])

        result = adapter.render_approval_card(card)
        expected = build_approval_message_from_specs(draft, [])

        assert result == expected

    def test_parity_with_deep_link_button(self, adapter):
        from router.approvals.block_kit import ACTION_OPEN_IN_APP

        draft = _make_draft()
        specs = [
            ButtonSpec(
                action_id=ACTION_OPEN_IN_APP,
                text="Open in Outlook",
                style="primary",
                url="https://outlook.office.com/mail/drafts/id/abc123",
            ),
            ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
        ]
        card = _make_card(draft, specs)

        result = adapter.render_approval_card(card)
        expected = build_approval_message_from_specs(draft, specs)

        assert result == expected

    def test_parity_with_calendar_payload(self, adapter):
        draft = _make_draft(
            capability_type="calendar",
            action_verb="book",
            payload={
                "title": "Team standup",
                "attendees": ["alice@co.com", "bob@co.com"],
                "start_time": "2026-01-20 10:00 AM",
            },
        )
        specs = [
            ButtonSpec(action_id=ACTION_APPROVE_SEND, text="Book", style="primary"),
            ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
        ]
        card = _make_card(draft, specs)

        result = adapter.render_approval_card(card)
        expected = build_approval_message_from_specs(draft, specs)

        assert result == expected


# ---------------------------------------------------------------------------
# Structural rendering tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackAdapterRender:
    def test_render_includes_agent_and_verb_in_header(self, adapter):
        draft = _make_draft(agent_name="lisa", action_verb="send")
        card = _make_card(draft)
        result = adapter.render_approval_card(card)

        header = result["blocks"][0]
        assert header["type"] == "header"
        assert "Lisa" in header["text"]["text"]
        assert "send" in header["text"]["text"]

    def test_render_includes_capability_in_context(self, adapter):
        draft = _make_draft(capability_type="email", capability_instance="mine")
        card = _make_card(draft)
        result = adapter.render_approval_card(card)

        context = result["blocks"][1]
        assert "mine" in context["elements"][0]["text"]
        assert "email" in context["elements"][0]["text"]

    def test_render_buttons_carry_draft_id(self, adapter):
        draft = _make_draft(draft_id="parity-test-001")
        card = _make_card(draft)
        result = adapter.render_approval_card(card)

        actions_block = result["blocks"][-1]
        assert actions_block["type"] == "actions"
        assert actions_block["block_id"] == "approval_parity-test-001"
        for btn in actions_block["elements"]:
            assert btn["value"] == "parity-test-001"

    def test_render_primary_button_has_style(self, adapter):
        draft = _make_draft()
        specs = [ButtonSpec(action_id=ACTION_APPROVE_SEND, text="Send", style="primary")]
        card = _make_card(draft, specs)
        result = adapter.render_approval_card(card)

        btn = result["blocks"][-1]["elements"][0]
        assert btn["style"] == "primary"

    def test_render_discard_button_is_danger(self, adapter):
        draft = _make_draft()
        specs = [ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger")]
        card = _make_card(draft, specs)
        result = adapter.render_approval_card(card)

        btn = result["blocks"][-1]["elements"][0]
        assert btn["style"] == "danger"

    def test_render_card_with_expires_at_does_not_error(self, adapter):
        draft = _make_draft(expires_at=datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc))
        card = _make_card(draft)
        result = adapter.render_approval_card(card)

        assert "blocks" in result


# ---------------------------------------------------------------------------
# ApprovalCard DTO tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApprovalCard:
    def test_requires_followup_defaults_to_false(self):
        draft = _make_draft()
        card = _make_card(draft)
        assert card.requires_followup is False

    def test_requires_followup_can_be_set(self):
        draft = _make_draft()
        card = _make_card(draft)
        card.requires_followup = True
        assert card.requires_followup is True

    def test_pack_can_be_none_for_connector_backed(self):
        draft = _make_draft()
        card = _make_card(draft)
        card.pack = None
        assert card.pack is None

    def test_pack_carries_pack_name(self):
        draft = _make_draft()
        card = ApprovalCard(
            draft_id=draft.draft_id,
            agent_name=draft.agent_name,
            pack="github",
            capability_type=draft.capability_type,
            capability_instance=draft.capability_instance,
            action_verb=draft.action_verb,
            summary="merge PR #42",
            payload=draft.payload,
            actions=[],
        )
        assert card.pack == "github"

    def test_summary_field_is_accessible(self):
        draft = _make_draft()
        card = ApprovalCard(
            draft_id=draft.draft_id,
            agent_name=draft.agent_name,
            pack=None,
            capability_type=draft.capability_type,
            capability_instance=draft.capability_instance,
            action_verb=draft.action_verb,
            summary="To: recruiter@example.com | Subject: Re: Engineering position",
            payload=draft.payload,
            actions=[],
        )
        assert "recruiter@example.com" in card.summary


# ---------------------------------------------------------------------------
# E2E callback path: render card → on_approval → verify state + callbacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestE2ECallbackPath:
    @pytest.mark.asyncio
    async def test_render_then_approve_transitions_draft(self, adapter, store):
        """Full path: render an approval card → approve via on_approval → draft approved."""
        draft = _make_draft()
        store.create(draft)
        card = _make_card(draft)

        # Render step (e.g. what internal_api does on POST /internal/drafts)
        rendered = adapter.render_approval_card(card)
        assert "blocks" in rendered  # card rendered cleanly

        # Approval step (e.g. what happens when user clicks the button)
        result = await on_approval(draft.draft_id, "approved", store)

        assert result is not None
        assert result.status == "approved"
        assert store.get(draft.draft_id).status == "approved"

    @pytest.mark.asyncio
    async def test_render_then_discard_native_invokes_cleanup(self, adapter, store):
        """Full path: render card → user discards → cleanup callback fires."""
        draft = _make_draft(draft_type="native", external_id="msg-graph-id-001")
        store.create(draft)
        card = _make_card(draft)

        rendered = adapter.render_approval_card(card)
        assert "blocks" in rendered

        cleanup = AsyncMock()
        result = await on_approval(draft.draft_id, "discarded", store, cleanup_callback=cleanup)

        assert result is not None
        assert result.status == "discarded"
        cleanup.assert_awaited_once()
        assert cleanup.call_args[0][0].external_id == "msg-graph-id-001"

    @pytest.mark.asyncio
    async def test_expired_card_approval_is_noop(self, adapter, store):
        """Approval action on an expired draft is silently ignored."""
        draft = _make_draft(status="expired")
        store.create(draft)
        card = _make_card(draft)

        rendered = adapter.render_approval_card(card)
        assert "blocks" in rendered

        result = await on_approval(draft.draft_id, "approved", store)

        assert result is None
        assert store.get(draft.draft_id).status == "expired"  # unchanged

    @pytest.mark.asyncio
    async def test_stale_draft_discard_does_not_call_cleanup(self, adapter, store):
        """Discarding an already-resolved draft must not trigger external cleanup."""
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")  # already resolved

        card = _make_card(draft)
        adapter.render_approval_card(card)  # render still works (no side effects)

        cleanup = AsyncMock()
        result = await on_approval(draft.draft_id, "discarded", store, cleanup_callback=cleanup)

        assert result is None
        cleanup.assert_not_awaited()
