"""Tests for approval flow Slack interactivity handlers.

Simulates Slack interactivity payloads and verifies that:
- The correct handler is invoked
- The draft store is updated
- The Slack message is edited with the outcome
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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

    @pytest.mark.asyncio
    async def test_approve_direct_draft_invokes_execute_callback(self, store):
        """Direct (pack-backed) drafts trigger the execute callback so the
        agent actually runs the approved action — this is the fix for
        ‘approval card said approved but the PR never merged’."""
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        execute = AsyncMock()
        mock_app = MagicMock()
        mock_app.action = MagicMock(return_value=lambda f: f)
        register_handlers(mock_app, store, execute_callback=execute)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        execute.assert_awaited_once()
        called_draft, called_channel, called_thread, called_client = execute.call_args.args
        assert called_draft.draft_id == draft.draft_id
        assert called_channel == "C12345"
        # message.thread_ts isn't in the test body so we fall back to message.ts
        assert called_thread == "1705700000.000100"

    @pytest.mark.asyncio
    async def test_approve_native_draft_skips_execute_callback(self, store):
        """Native (connector-backed) drafts already exist in the external
        app — the user finishes them there, not via re-dispatch."""
        draft = _make_draft(draft_type="native", external_id="ext-123")
        store.create(draft)

        execute = AsyncMock()
        mock_app = MagicMock()
        mock_app.action = MagicMock(return_value=lambda f: f)
        register_handlers(mock_app, store, execute_callback=execute)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_execute_callback_failure_does_not_revert_status(self, store):
        """If the agent's execute step blows up, we keep the draft marked
        approved — the human did approve. The error is logged and the
        callback is responsible for surfacing it to Slack."""
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        execute = AsyncMock(side_effect=RuntimeError("agent dispatch crashed"))
        mock_app = MagicMock()
        mock_app.action = MagicMock(return_value=lambda f: f)
        register_handlers(mock_app, store, execute_callback=execute)

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        assert store.get(draft.draft_id).status == "approved"
        execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approve_toctou_expired_no_crash_and_posts_notice(self, store):
        """TOCTOU: expiration worker flips pending→expired between the status check
        and store.transition.  The handler must not raise and must post a friendly notice."""
        draft = _make_draft()
        store.create(draft)

        def expire_then_raise(draft_id, new_status):
            store._conn.execute("UPDATE drafts SET status = 'expired' WHERE draft_id = ?", (draft_id,))
            store._conn.commit()
            raise ValueError(f"Cannot transition from 'expired' to '{new_status}'")

        with patch.object(store, "transition", side_effect=expire_then_raise):
            ack = AsyncMock()
            client = AsyncMock()
            body = _make_action_body(draft.draft_id)

            await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "expired" in text.lower()

    @pytest.mark.asyncio
    async def test_approve_double_click_no_crash_and_posts_notice(self, store):
        """Double-click: second approve on an already-approved draft is graceful."""
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id)

        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "approved" in text.lower()


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

    @pytest.mark.asyncio
    async def test_discard_toctou_expired_no_crash_and_posts_notice(self, store):
        """TOCTOU: expiration worker flips pending→expired between the status check
        and store.transition.  The discard handler must not raise and must post a notice."""
        draft = _make_draft()
        store.create(draft)

        def expire_then_raise(draft_id, new_status):
            store._conn.execute("UPDATE drafts SET status = 'expired' WHERE draft_id = ?", (draft_id,))
            store._conn.commit()
            raise ValueError(f"Cannot transition from 'expired' to '{new_status}'")

        with patch.object(store, "transition", side_effect=expire_then_raise):
            ack = AsyncMock()
            client = AsyncMock()
            body = _make_action_body(draft.draft_id, ACTION_DISCARD)

            await _handle_discard(ack, body, client)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "expired" in text.lower()

    @pytest.mark.asyncio
    async def test_discard_double_click_no_crash_and_posts_notice(self, store):
        """Double-click: second discard on an already-discarded draft is graceful."""
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "discarded")

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_DISCARD)

        await _handle_discard(ack, body, client)

        ack.assert_awaited_once()
        client.chat_update.assert_not_awaited()
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "discarded" in text.lower()


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

    @pytest.mark.asyncio
    async def test_edit_already_resolved_posts_notice(self, store):
        """Clicking Edit on an already-resolved draft posts a friendly notice instead of silently skipping."""
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")

        ack = AsyncMock()
        client = AsyncMock()
        body = _make_action_body(draft.draft_id, ACTION_REQUEST_EDIT)

        await _handle_request_edit(ack, body, client)

        ack.assert_awaited_once()
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "approved" in text.lower()


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
