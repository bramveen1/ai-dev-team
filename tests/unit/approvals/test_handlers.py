"""Tests for approval flow Slack interactivity handlers.

Simulates Slack interactivity payloads and verifies that:
- The correct handler is invoked
- The draft store is updated
- The Slack message is edited with the outcome
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_REQUEST_EDIT,
)
from router.approvals.handlers import (
    _handle_approve,
    _handle_discard,
    _handle_request_edit,
    register_handlers,
)
from router.approvals.store import Draft, DraftStore


def _make_draft(**overrides) -> Draft:
    """Create a Draft with sensible defaults."""
    defaults = {
        "draft_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "capability_type": "email",
        "capability_instance": "mine",
        "action_verb": "send",
        "payload": {"to": "user@example.com", "subject": "Hello", "body": "Hi there!"},
        "slack_channel": "C12345",
        "slack_message_ts": "1705700000.000100",
        "status": "pending",
        "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Draft(**defaults)


def _make_action_body(draft_id: str, action_id: str = ACTION_APPROVE_SEND) -> dict:
    """Build a simulated Slack interactivity payload body."""
    return {
        "actions": [{"action_id": action_id, "value": draft_id}],
        "channel": {"id": "C12345"},
        "message": {"ts": "1705700000.000100"},
        "user": {"id": "U0001"},
    }


@pytest.fixture
def store(tmp_path):
    """Create a DraftStore for testing."""
    db_path = str(tmp_path / "test_handlers.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _register(store):
    """Register handlers with a mock bolt app before each test."""
    mock_app = MagicMock()
    mock_app.action = MagicMock(return_value=lambda f: f)
    register_handlers(mock_app, store)


@pytest.mark.unit
class TestHandleApprove:
    @pytest.mark.asyncio
    async def test_approve_send_transitions_to_approved(self, store):
        draft = _make_draft()
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_APPROVE_SEND)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        ack.assert_awaited_once()

        result = store.get(draft.draft_id)
        assert result.status == "approved"
        assert result.resolved_at is not None

    @pytest.mark.asyncio
    async def test_approve_updates_slack_message(self, store):
        draft = _make_draft()
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        client.chat_update.assert_awaited_once()
        call_kwargs = client.chat_update.call_args.kwargs
        assert call_kwargs["channel"] == "C12345"
        assert call_kwargs["ts"] == "1705700000.000100"
        assert "blocks" in call_kwargs

    @pytest.mark.asyncio
    async def test_approve_publish_works(self, store):
        draft = _make_draft(action_verb="publish", capability_type="social")
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_APPROVE_PUBLISH)

        await _handle_approve(ack, body, client, ACTION_APPROVE_PUBLISH)

        assert store.get(draft.draft_id).status == "approved"

    @pytest.mark.asyncio
    async def test_approve_book_works(self, store):
        draft = _make_draft(action_verb="book", capability_type="calendar")
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_APPROVE_BOOK)

        await _handle_approve(ack, body, client, ACTION_APPROVE_BOOK)

        assert store.get(draft.draft_id).status == "approved"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_draft_is_noop(self, store):
        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body("nonexistent-id")

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_already_resolved_is_noop(self, store):
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()


@pytest.mark.unit
class TestHandleDiscard:
    @pytest.mark.asyncio
    async def test_discard_transitions_to_discarded(self, store):
        draft = _make_draft()
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        ack.assert_awaited_once()

        result = store.get(draft.draft_id)
        assert result.status == "discarded"
        assert result.resolved_at is not None

    @pytest.mark.asyncio
    async def test_discard_updates_slack_message(self, store):
        draft = _make_draft()
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        client.chat_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discard_nonexistent_is_noop(self, store):
        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body("nonexistent-id", ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discard_native_draft_invokes_cleanup_callback(self, store):
        """Discarding a native draft (e.g. M365) should call the cleanup callback."""
        draft = _make_draft(
            draft_type="native",
            external_id="AAMkAGI2TG93AAA=",
            capability_instance="bram",
        )
        store.create(draft)

        cleanup = AsyncMock()
        mock_app = MagicMock()
        mock_app.action = MagicMock(return_value=lambda f: f)
        register_handlers(mock_app, store, cleanup_callback=cleanup)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        cleanup.assert_awaited_once()
        cleanup_draft = cleanup.call_args[0][0]
        assert cleanup_draft.external_id == "AAMkAGI2TG93AAA="
        assert store.get(draft.draft_id).status == "discarded"

    @pytest.mark.asyncio
    async def test_discard_direct_draft_does_not_invoke_cleanup(self, store):
        """Discarding a direct draft should NOT call the cleanup callback."""
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        cleanup = AsyncMock()
        mock_app = MagicMock()
        mock_app.action = MagicMock(return_value=lambda f: f)
        register_handlers(mock_app, store, cleanup_callback=cleanup)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        cleanup.assert_not_awaited()
        assert store.get(draft.draft_id).status == "discarded"

    @pytest.mark.asyncio
    async def test_discard_cleanup_failure_still_discards(self, store):
        """If cleanup callback fails, draft should still be marked as discarded."""
        draft = _make_draft(draft_type="native", external_id="fail-draft")
        store.create(draft)

        cleanup = AsyncMock(side_effect=RuntimeError("Graph API timeout"))
        mock_app = MagicMock()
        mock_app.action = MagicMock(return_value=lambda f: f)
        register_handlers(mock_app, store, cleanup_callback=cleanup)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        assert store.get(draft.draft_id).status == "discarded"


@pytest.mark.unit
class TestHandleRequestEdit:
    @pytest.mark.asyncio
    async def test_edit_posts_thread_reply(self, store):
        draft = _make_draft()
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_REQUEST_EDIT)

        await _handle_request_edit(ack, body, client)

        ack.assert_awaited_once()
        client.chat_postMessage.assert_awaited_once()

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C12345"
        assert call_kwargs["thread_ts"] == "1705700000.000100"
        assert "changes" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_edit_does_not_change_status(self, store):
        draft = _make_draft()
        store.create(draft)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_REQUEST_EDIT)

        await _handle_request_edit(ack, body, client)

        assert store.get(draft.draft_id).status == "pending"


@pytest.mark.unit
class TestRegisterHandlers:
    def test_registers_all_action_ids(self):
        mock_app = MagicMock()
        registered_actions = []
        mock_app.action = MagicMock(side_effect=lambda action_id: registered_actions.append(action_id) or (lambda f: f))

        store = MagicMock(spec=DraftStore)
        register_handlers(mock_app, store)

        expected_actions = {
            ACTION_APPROVE_SEND,
            ACTION_APPROVE_PUBLISH,
            ACTION_APPROVE_BOOK,
            ACTION_DISCARD,
            ACTION_REQUEST_EDIT,
        }
        assert set(registered_actions) == expected_actions
