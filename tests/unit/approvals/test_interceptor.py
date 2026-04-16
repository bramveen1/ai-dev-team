"""Tests for the approval flow response interceptor.

Verifies that:
- draft-approval code-fence blocks are correctly parsed from agent responses
- Malformed or incomplete blocks are silently stripped
- Approval messages are posted with permission-aware buttons
- Draft records are persisted with correct fields
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from router.approvals.block_kit import ACTION_APPROVE_SEND, ACTION_DISCARD, ACTION_OPEN_IN_APP, ACTION_REQUEST_EDIT
from router.approvals.capabilities_loader import CapabilityInstance
from router.approvals.interceptor import (
    DraftRequest,
    parse_response,
    post_approval_message,
)
from router.approvals.store import DraftStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_interceptor.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture
def slack_client():
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1705700000.000200"}
    return client


def _make_draft_request(**overrides) -> DraftRequest:
    defaults = {
        "draft_id": "AAMkAGI2TG93AAA=",
        "capability_type": "email",
        "capability_instance": "bram",
        "action_verb": "send",
        "payload": {"to": "sam@example.com", "subject": "Test", "body": "Hello"},
    }
    defaults.update(overrides)
    return DraftRequest(**defaults)


def _make_capability_instance(**overrides) -> CapabilityInstance:
    defaults = {
        "instance": "bram",
        "provider": "m365-mcp",
        "account": "bram@pathtohired.com",
        "ownership": "delegate",
        "permissions": ["read", "draft-create", "draft-update", "draft-delete"],
    }
    defaults.update(overrides)
    return CapabilityInstance(**defaults)


# ---------------------------------------------------------------------------
# parse_response tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseResponse:
    def test_no_draft_blocks(self):
        text = "Done! I've sent the email."
        result = parse_response(text)

        assert result.cleaned_text == text
        assert result.has_drafts is False
        assert result.draft_requests == []

    def test_single_draft_block(self):
        payload = {
            "draft_id": "AAMkAGI2TG93AAA=",
            "capability_type": "email",
            "capability_instance": "bram",
            "action_verb": "send",
            "payload": {"to": "sam@example.com", "subject": "Test", "body": "Hi"},
        }
        text = f"Done — drafted that email for you.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        assert len(result.draft_requests) == 1
        assert result.draft_requests[0].draft_id == "AAMkAGI2TG93AAA="
        assert result.draft_requests[0].capability_type == "email"
        assert result.draft_requests[0].capability_instance == "bram"
        assert result.draft_requests[0].action_verb == "send"
        assert result.draft_requests[0].payload == {"to": "sam@example.com", "subject": "Test", "body": "Hi"}
        assert "draft-approval" not in result.cleaned_text
        assert result.cleaned_text == "Done — drafted that email for you."

    def test_multiple_draft_blocks(self):
        payload1 = {
            "draft_id": "draft-1",
            "capability_type": "email",
            "capability_instance": "bram",
            "action_verb": "send",
            "payload": {"to": "a@example.com", "subject": "First"},
        }
        payload2 = {
            "draft_id": "draft-2",
            "capability_type": "email",
            "capability_instance": "mine",
            "action_verb": "send",
            "payload": {"to": "b@example.com", "subject": "Second"},
        }
        text = (
            f"Created both drafts.\n\n"
            f"```draft-approval\n{json.dumps(payload1)}\n```\n\n"
            f"```draft-approval\n{json.dumps(payload2)}\n```"
        )

        result = parse_response(text)

        assert len(result.draft_requests) == 2
        assert result.draft_requests[0].draft_id == "draft-1"
        assert result.draft_requests[1].draft_id == "draft-2"
        assert result.cleaned_text == "Created both drafts."

    def test_malformed_json_is_stripped(self):
        text = "Here you go.\n\n```draft-approval\n{this is not json}\n```"

        result = parse_response(text)

        assert result.has_drafts is False
        assert result.cleaned_text == "Here you go."

    def test_missing_required_fields_is_stripped(self):
        incomplete = {"draft_id": "AAMk...", "capability_type": "email"}
        text = f"Done.\n\n```draft-approval\n{json.dumps(incomplete)}\n```"

        result = parse_response(text)

        assert result.has_drafts is False
        assert result.cleaned_text == "Done."

    def test_payload_must_be_dict(self):
        payload = {
            "draft_id": "x",
            "capability_type": "email",
            "capability_instance": "bram",
            "action_verb": "send",
            "payload": "not a dict",
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        assert result.draft_requests[0].payload == {}

    def test_preserves_surrounding_text(self):
        payload = {
            "draft_id": "x",
            "capability_type": "email",
            "capability_instance": "bram",
            "action_verb": "send",
            "payload": {"to": "a@b.com"},
        }
        text = f"First line.\n\nMiddle text.\n\n```draft-approval\n{json.dumps(payload)}\n```\n\nAfter text."

        result = parse_response(text)

        assert "First line." in result.cleaned_text
        assert "Middle text." in result.cleaned_text
        assert "After text." in result.cleaned_text

    def test_regular_code_blocks_are_not_matched(self):
        text = "Here's some code:\n\n```python\nprint('hello')\n```\n\nDone."

        result = parse_response(text)

        assert result.has_drafts is False
        assert "```python" in result.cleaned_text


# ---------------------------------------------------------------------------
# post_approval_message tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPostApprovalMessage:
    @pytest.mark.asyncio
    async def test_native_draft_when_no_send_permission(self, store, slack_client):
        """M365 delegate account without send permission → native draft with deep link."""
        draft_req = _make_draft_request()
        cap_instance = _make_capability_instance(permissions=["read", "draft-create", "draft-update", "draft-delete"])

        draft = await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            capability_instance=cap_instance,
        )

        assert draft.draft_type == "native"
        assert draft.external_id == "AAMkAGI2TG93AAA="
        assert draft.status == "pending"
        assert draft.agent_name == "lisa"
        assert draft.capability_type == "email"
        assert draft.capability_instance == "bram"
        assert draft.action_verb == "send"
        assert draft.expires_at is not None

        # Verify Slack message was posted
        slack_client.chat_postMessage.assert_awaited_once()
        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C12345"
        assert call_kwargs["thread_ts"] == "1705700000.000100"
        assert "blocks" in call_kwargs

        # Verify buttons include "Open in Outlook" (not "Send")
        blocks = call_kwargs["blocks"]
        actions_block = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions_block) == 1
        button_actions = [e["action_id"] for e in actions_block[0]["elements"]]
        assert ACTION_OPEN_IN_APP in button_actions
        assert ACTION_APPROVE_SEND not in button_actions
        assert ACTION_DISCARD in button_actions

        # Verify draft was persisted in store
        persisted = store.get(draft.draft_id)
        assert persisted is not None
        assert persisted.draft_type == "native"
        assert persisted.slack_message_ts == "1705700000.000200"

    @pytest.mark.asyncio
    async def test_direct_draft_when_has_send_permission(self, store, slack_client):
        """Zoho self account with send permission → direct draft with Send button."""
        draft_req = _make_draft_request(
            draft_id="zoho-draft-123",
            capability_instance="mine",
        )
        cap_instance = _make_capability_instance(
            instance="mine",
            provider="zoho-mcp",
            account="lisa@pathtohired.com",
            ownership="self",
            permissions=["read", "send", "draft-create", "draft-update", "draft-delete"],
        )

        draft = await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            capability_instance=cap_instance,
        )

        assert draft.draft_type == "direct"
        assert draft.external_id is None

        # Verify buttons include "Send" (not "Open in...")
        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        actions_block = [b for b in blocks if b.get("type") == "actions"]
        button_actions = [e["action_id"] for e in actions_block[0]["elements"]]
        assert ACTION_APPROVE_SEND in button_actions
        assert ACTION_OPEN_IN_APP not in button_actions
        assert ACTION_REQUEST_EDIT in button_actions
        assert ACTION_DISCARD in button_actions

    @pytest.mark.asyncio
    async def test_no_capability_instance_falls_back_to_discard_only(self, store, slack_client):
        """When capability instance is unknown, only show Discard button."""
        draft_req = _make_draft_request()

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            capability_instance=None,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        actions_block = [b for b in blocks if b.get("type") == "actions"]
        button_actions = [e["action_id"] for e in actions_block[0]["elements"]]
        assert button_actions == [ACTION_DISCARD]

    @pytest.mark.asyncio
    async def test_draft_persisted_with_slack_message_ts(self, store, slack_client):
        """Draft record should have the Slack message_ts from the posted approval message."""
        slack_client.chat_postMessage.return_value = {"ts": "1705700099.000300"}

        draft_req = _make_draft_request()
        cap_instance = _make_capability_instance()

        draft = await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C99",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            capability_instance=cap_instance,
        )

        persisted = store.get(draft.draft_id)
        assert persisted.slack_message_ts == "1705700099.000300"
        assert persisted.slack_channel == "C99"

    @pytest.mark.asyncio
    async def test_deep_link_url_for_m365_native_draft(self, store, slack_client):
        """M365 native draft should have an Outlook deep link URL on the Open button."""
        draft_req = _make_draft_request(draft_id="AAMkTest123")
        cap_instance = _make_capability_instance()

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            capability_instance=cap_instance,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        actions_block = [b for b in blocks if b.get("type") == "actions"][0]
        open_button = [e for e in actions_block["elements"] if e["action_id"] == ACTION_OPEN_IN_APP][0]
        assert "outlook.office.com" in open_button["url"]
        assert "AAMkTest123" in open_button["url"]

    @pytest.mark.asyncio
    async def test_payload_preview_in_approval_message(self, store, slack_client):
        """Approval message should contain a preview of the email content."""
        draft_req = _make_draft_request(
            payload={"to": "sam@example.com", "subject": "Hello Sam", "body": "How are you?"},
        )
        cap_instance = _make_capability_instance()

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            capability_instance=cap_instance,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        blocks = call_kwargs["blocks"]
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        section_text = " ".join(b["text"]["text"] for b in section_blocks)
        assert "sam@example.com" in section_text
        assert "Hello Sam" in section_text
        assert "How are you?" in section_text
