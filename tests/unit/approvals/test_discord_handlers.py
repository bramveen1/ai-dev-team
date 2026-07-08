"""Tests for approval flow Discord interactivity handlers (#682).

Simulates discord.py component-interaction payloads and verifies that:
- The correct handler is invoked by custom_id
- The draft store is updated
- The Discord message is edited with the outcome
- The execute callback fires for direct drafts, parity with the Slack handler
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
from router.approvals.discord_handlers import (
    _handle_approve,
    _handle_discard,
    _handle_request_edit,
    _on_interaction,
    _parse_custom_id,
    register_handlers,
)
from router.approvals.store import Draft, DraftStore

pytestmark = pytest.mark.unit


def _make_draft(**overrides) -> Draft:
    defaults = {
        "draft_id": str(uuid.uuid4()),
        "agent_name": "sam",
        "capability_type": "pack",
        "capability_instance": "dispatch",
        "action_verb": "dispatch_issue",
        "payload": {
            "issue_url": "https://github.com/o/r/issues/1",
            "transport": "discord",
            "conversation_id": "discord:1:2:3",
        },
        "slack_channel": "discord:1:2:3",
        "slack_message_ts": "999",
        "status": "pending",
        "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Draft(**defaults)


def _make_interaction(custom_id: str, interaction_type=None) -> MagicMock:
    import discord

    interaction = MagicMock()
    interaction.type = interaction_type if interaction_type is not None else discord.InteractionType.component
    interaction.data = {"custom_id": custom_id}
    interaction.response = AsyncMock()
    return interaction


def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.register_interaction_handler = MagicMock()
    return adapter


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_discord_handlers.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _register(store):
    register_handlers(_make_adapter(), store)


class TestParseCustomId:
    def test_valid_custom_id(self):
        assert _parse_custom_id(f"approval:{ACTION_APPROVE_SEND}:abc123") == (ACTION_APPROVE_SEND, "abc123")

    @pytest.mark.parametrize(
        "custom_id",
        [
            "not_approval:approve_send:abc123",
            "approval:approve_send",
            "approval::abc123",
            "approval:approve_send:",
            "",
        ],
    )
    def test_invalid_custom_ids_return_none(self, custom_id):
        assert _parse_custom_id(custom_id) is None


class TestRegisterHandlers:
    def test_registers_interaction_listener_on_adapter(self, store):
        adapter = _make_adapter()
        register_handlers(adapter, store)
        adapter.register_interaction_handler.assert_called_once_with(_on_interaction)


class TestOnInteractionRouting:
    @pytest.mark.asyncio
    async def test_ignores_non_component_interactions(self, store):
        import discord

        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(
            f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}",
            interaction_type=discord.InteractionType.application_command,
        )

        await _on_interaction(interaction)

        interaction.response.edit_message.assert_not_awaited()
        assert store.get(draft.draft_id).status == "pending"

    @pytest.mark.asyncio
    async def test_ignores_foreign_custom_ids(self, store):
        interaction = _make_interaction("some_other_component:xyz")
        await _on_interaction(interaction)
        interaction.response.edit_message.assert_not_awaited()
        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_action_routes_to_handle_approve(self, store):
        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}")

        await _on_interaction(interaction)

        assert store.get(draft.draft_id).status == "approved"
        interaction.response.edit_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discard_action_routes_to_handle_discard(self, store):
        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(f"approval:{ACTION_DISCARD}:{draft.draft_id}")

        await _on_interaction(interaction)

        assert store.get(draft.draft_id).status == "discarded"

    @pytest.mark.asyncio
    async def test_request_edit_routes_to_handle_request_edit(self, store):
        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(f"approval:{ACTION_REQUEST_EDIT}:{draft.draft_id}")

        await _on_interaction(interaction)

        assert store.get(draft.draft_id).status == "pending"
        interaction.response.send_message.assert_awaited_once()


class TestHandleApprove:
    @pytest.mark.asyncio
    async def test_approve_transitions_to_approved_and_edits_message(self, store):
        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}")

        await _handle_approve(interaction, ACTION_APPROVE_SEND, draft.draft_id)

        result = store.get(draft.draft_id)
        assert result.status == "approved"
        assert result.resolved_at is not None
        interaction.response.edit_message.assert_awaited_once()
        call_kwargs = interaction.response.edit_message.call_args.kwargs
        assert call_kwargs["view"] is None
        assert "embed" in call_kwargs

    @pytest.mark.asyncio
    async def test_approve_publish_and_book_work(self, store):
        for action_id, verb in ((ACTION_APPROVE_PUBLISH, "publish"), (ACTION_APPROVE_BOOK, "book")):
            draft = _make_draft(action_verb=verb)
            store.create(draft)
            interaction = _make_interaction(f"approval:{action_id}:{draft.draft_id}")

            await _handle_approve(interaction, action_id, draft.draft_id)

            assert store.get(draft.draft_id).status == "approved"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_draft_is_noop(self, store):
        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:nonexistent")

        await _handle_approve(interaction, ACTION_APPROVE_SEND, "nonexistent")

        interaction.response.edit_message.assert_not_awaited()
        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_already_resolved_sends_ephemeral_notice(self, store):
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")
        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}")

        await _handle_approve(interaction, ACTION_APPROVE_SEND, draft.draft_id)

        interaction.response.edit_message.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_approve_direct_draft_invokes_execute_callback(self, store):
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        execute = AsyncMock()
        register_handlers(_make_adapter(), store, execute_callback=execute)

        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}")
        await _handle_approve(interaction, ACTION_APPROVE_SEND, draft.draft_id)

        execute.assert_awaited_once()
        called_draft, called_interaction = execute.call_args.args
        assert called_draft.draft_id == draft.draft_id
        assert called_interaction is interaction

    @pytest.mark.asyncio
    async def test_approve_native_draft_skips_execute_callback(self, store):
        draft = _make_draft(draft_type="native", external_id="ext-123")
        store.create(draft)

        execute = AsyncMock()
        register_handlers(_make_adapter(), store, execute_callback=execute)

        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}")
        await _handle_approve(interaction, ACTION_APPROVE_SEND, draft.draft_id)

        execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_callback_exception_does_not_propagate(self, store):
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        execute = AsyncMock(side_effect=RuntimeError("boom"))
        register_handlers(_make_adapter(), store, execute_callback=execute)

        interaction = _make_interaction(f"approval:{ACTION_APPROVE_SEND}:{draft.draft_id}")
        await _handle_approve(interaction, ACTION_APPROVE_SEND, draft.draft_id)  # must not raise

        assert store.get(draft.draft_id).status == "approved"


class TestHandleDiscard:
    @pytest.mark.asyncio
    async def test_discard_transitions_to_discarded_and_edits_message(self, store):
        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(f"approval:{ACTION_DISCARD}:{draft.draft_id}")

        await _handle_discard(interaction, draft.draft_id)

        assert store.get(draft.draft_id).status == "discarded"
        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.call_args.kwargs["view"] is None

    @pytest.mark.asyncio
    async def test_discard_invokes_cleanup_callback_for_native_drafts(self, store):
        draft = _make_draft(draft_type="native", external_id="ext-123")
        store.create(draft)

        cleanup = AsyncMock()
        register_handlers(_make_adapter(), store, cleanup_callback=cleanup)

        interaction = _make_interaction(f"approval:{ACTION_DISCARD}:{draft.draft_id}")
        await _handle_discard(interaction, draft.draft_id)

        cleanup.assert_awaited_once()
        assert store.get(draft.draft_id).status == "discarded"

    @pytest.mark.asyncio
    async def test_discard_already_resolved_sends_ephemeral_notice(self, store):
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "discarded")
        interaction = _make_interaction(f"approval:{ACTION_DISCARD}:{draft.draft_id}")

        await _handle_discard(interaction, draft.draft_id)

        interaction.response.edit_message.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()


class TestHandleRequestEdit:
    @pytest.mark.asyncio
    async def test_request_edit_posts_prompt_and_leaves_draft_pending(self, store):
        draft = _make_draft()
        store.create(draft)
        interaction = _make_interaction(f"approval:{ACTION_REQUEST_EDIT}:{draft.draft_id}")

        await _handle_request_edit(interaction, draft.draft_id)

        assert store.get(draft.draft_id).status == "pending"
        interaction.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_request_edit_nonexistent_draft_is_noop(self, store):
        interaction = _make_interaction(f"approval:{ACTION_REQUEST_EDIT}:nonexistent")

        await _handle_request_edit(interaction, "nonexistent")

        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_edit_already_resolved_sends_ephemeral_notice(self, store):
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")
        interaction = _make_interaction(f"approval:{ACTION_REQUEST_EDIT}:{draft.draft_id}")

        await _handle_request_edit(interaction, draft.draft_id)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
