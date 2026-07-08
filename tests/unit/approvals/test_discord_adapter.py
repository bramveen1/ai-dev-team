"""Tests for DiscordApprovalAdapter — approval card rendering (#682).

Covers:
- render_approval_card: embed + interactive button row (components)
- render_components: custom_id encoding, style mapping, link buttons
- render_outcome_embed: post-decision message content
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from router.approvals.adapters.discord import CUSTOM_ID_PREFIX, DiscordApprovalAdapter
from router.approvals.block_kit import ACTION_APPROVE_SEND, ACTION_DISCARD, ACTION_OPEN_IN_APP, ACTION_REQUEST_EDIT
from router.approvals.button_resolver import ButtonSpec
from router.approvals.card import ApprovalCard
from router.approvals.store import Draft

pytestmark = pytest.mark.unit


def _make_draft(**overrides) -> Draft:
    defaults = {
        "draft_id": str(uuid.uuid4()),
        "agent_name": "sam",
        "capability_type": "pack",
        "capability_instance": "dispatch",
        "action_verb": "dispatch_issue",
        "payload": {"issue_url": "https://github.com/o/r/issues/1"},
        "slack_channel": "discord:1:2:3",
        "slack_message_ts": "999",
        "status": "pending",
        "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Draft(**defaults)


def _make_card(draft: Draft, actions: list | None = None) -> ApprovalCard:
    if actions is None:
        actions = [
            ButtonSpec(action_id=ACTION_APPROVE_SEND, text="Dispatch_issue", style="primary"),
            ButtonSpec(action_id=ACTION_REQUEST_EDIT, text="Edit"),
            ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
        ]
    return ApprovalCard(
        draft_id=draft.draft_id,
        agent_name=draft.agent_name,
        pack="dispatch",
        capability_type=draft.capability_type,
        capability_instance=draft.capability_instance,
        action_verb=draft.action_verb,
        summary="",
        payload=draft.payload,
        actions=actions,
        expires_at=draft.expires_at,
    )


@pytest.fixture
def adapter() -> DiscordApprovalAdapter:
    return DiscordApprovalAdapter()


class TestRenderApprovalCardHasInteractiveComponents:
    def test_components_present_alongside_embed_and_content(self, adapter):
        """The #682 bug: the card posted with no Approve button — render
        output must carry a components action row the caller can post."""
        draft = _make_draft()
        card = _make_card(draft)

        result = adapter.render_approval_card(card)

        assert "content" in result
        assert "embed" in result
        assert "components" in result
        assert len(result["components"]) == 1
        assert result["components"][0]["type"] == 1  # action row
        assert len(result["components"][0]["components"]) == 3


class TestRenderComponents:
    def test_button_custom_id_encodes_action_and_draft_id(self, adapter):
        draft = _make_draft()
        card = _make_card(draft)

        [row] = adapter.render_components(card)
        approve_button = next(b for b in row["components"] if b["custom_id"].split(":")[1] == ACTION_APPROVE_SEND)

        assert approve_button["custom_id"] == f"{CUSTOM_ID_PREFIX}:{ACTION_APPROVE_SEND}:{draft.draft_id}"
        assert approve_button["type"] == 2  # button
        assert approve_button["label"] == "Dispatch_issue"

    def test_style_mapping(self, adapter):
        draft = _make_draft()
        card = _make_card(draft)

        [row] = adapter.render_components(card)
        by_action = {b["custom_id"].split(":")[1]: b for b in row["components"]}

        assert by_action[ACTION_APPROVE_SEND]["style"] == 3  # primary -> Success
        assert by_action[ACTION_DISCARD]["style"] == 4  # danger -> Danger
        assert by_action[ACTION_REQUEST_EDIT]["style"] == 2  # default -> Secondary

    def test_link_action_renders_as_link_button_without_custom_id(self, adapter):
        draft = _make_draft()
        card = _make_card(
            draft,
            actions=[
                ButtonSpec(
                    action_id=ACTION_OPEN_IN_APP,
                    text="Open in Outlook",
                    style="primary",
                    url="https://outlook.example/draft/1",
                ),
            ],
        )

        [row] = adapter.render_components(card)
        [button] = row["components"]

        assert button["style"] == 5  # Link
        assert button["url"] == "https://outlook.example/draft/1"
        assert "custom_id" not in button

    def test_no_actions_returns_empty_components(self, adapter):
        draft = _make_draft()
        card = _make_card(draft, actions=[])

        assert adapter.render_components(card) == []


class TestRenderOutcomeEmbed:
    def test_approved_outcome(self):
        draft = _make_draft(status="approved", resolved_at=datetime(2026, 1, 15, 13, 30, 0, tzinfo=timezone.utc))

        embed = DiscordApprovalAdapter.render_outcome_embed(draft, approved=True)

        assert "Dispatch_issueed" in embed["description"]
        assert embed["footer"]["text"] == f"draft_id: {draft.draft_id}"

    def test_discarded_outcome(self):
        draft = _make_draft(status="discarded")

        embed = DiscordApprovalAdapter.render_outcome_embed(draft, approved=False)

        assert "Discarded" in embed["description"]
