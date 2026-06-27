"""Tests for the backend-agnostic on_approval routing function.

Covers: approve, discard, expired/stale draft (already-resolved guard),
and the cleanup callback path for native drafts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from router.approvals.core import on_approval
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


@pytest.fixture
def store(tmp_path):
    """DraftStore backed by a temporary SQLite database."""
    db_path = str(tmp_path / "test_core.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.mark.unit
class TestOnApprovalApprove:
    @pytest.mark.asyncio
    async def test_approve_transitions_draft_to_approved(self, store):
        draft = _make_draft()
        store.create(draft)

        result = await on_approval(draft.draft_id, "approved", store)

        assert result is not None
        assert result.status == "approved"
        assert result.resolved_at is not None

    @pytest.mark.asyncio
    async def test_approve_returns_updated_draft(self, store):
        draft = _make_draft()
        store.create(draft)

        result = await on_approval(draft.draft_id, "approved", store)

        assert result is not None
        assert result.draft_id == draft.draft_id
        assert result.action_verb == draft.action_verb

    @pytest.mark.asyncio
    async def test_approve_persists_to_store(self, store):
        draft = _make_draft()
        store.create(draft)

        await on_approval(draft.draft_id, "approved", store)

        persisted = store.get(draft.draft_id)
        assert persisted.status == "approved"

    @pytest.mark.asyncio
    async def test_approve_does_not_invoke_cleanup_callback(self, store):
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        cleanup = AsyncMock()
        await on_approval(draft.draft_id, "approved", store, cleanup_callback=cleanup)

        cleanup.assert_not_awaited()


@pytest.mark.unit
class TestOnApprovalDiscard:
    @pytest.mark.asyncio
    async def test_discard_transitions_draft_to_discarded(self, store):
        draft = _make_draft()
        store.create(draft)

        result = await on_approval(draft.draft_id, "discarded", store)

        assert result is not None
        assert result.status == "discarded"

    @pytest.mark.asyncio
    async def test_discard_native_draft_invokes_cleanup_callback(self, store):
        """Native drafts (e.g. M365) trigger the cleanup callback on discard."""
        draft = _make_draft(draft_type="native", external_id="AAMkAGI2TG93AAA=")
        store.create(draft)

        cleanup = AsyncMock()
        result = await on_approval(draft.draft_id, "discarded", store, cleanup_callback=cleanup)

        cleanup.assert_awaited_once()
        called_draft = cleanup.call_args[0][0]
        assert called_draft.external_id == "AAMkAGI2TG93AAA="
        assert result.status == "discarded"

    @pytest.mark.asyncio
    async def test_discard_direct_draft_does_not_invoke_cleanup(self, store):
        """Pack-backed (direct) drafts don't need external cleanup on discard."""
        draft = _make_draft(draft_type="direct")
        store.create(draft)

        cleanup = AsyncMock()
        await on_approval(draft.draft_id, "discarded", store, cleanup_callback=cleanup)

        cleanup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discard_cleanup_failure_still_discards(self, store):
        """Cleanup failures are logged but the draft still transitions to discarded."""
        draft = _make_draft(draft_type="native", external_id="fail-ext-id")
        store.create(draft)

        cleanup = AsyncMock(side_effect=RuntimeError("Graph API timeout"))
        result = await on_approval(draft.draft_id, "discarded", store, cleanup_callback=cleanup)

        cleanup.assert_awaited_once()
        assert result is not None
        assert result.status == "discarded"

    @pytest.mark.asyncio
    async def test_discard_without_cleanup_callback_works(self, store):
        draft = _make_draft(draft_type="native")
        store.create(draft)

        result = await on_approval(draft.draft_id, "discarded", store)

        assert result is not None
        assert result.status == "discarded"


@pytest.mark.unit
class TestOnApprovalGuards:
    @pytest.mark.asyncio
    async def test_nonexistent_draft_returns_none(self, store):
        result = await on_approval("nonexistent-draft-id", "approved", store)

        assert result is None

    @pytest.mark.asyncio
    async def test_already_approved_draft_returns_none(self, store):
        """Idempotent guard: acting on an already-resolved draft is a no-op."""
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")

        result = await on_approval(draft.draft_id, "approved", store)

        assert result is None

    @pytest.mark.asyncio
    async def test_already_discarded_draft_returns_none(self, store):
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "discarded")

        result = await on_approval(draft.draft_id, "discarded", store)

        assert result is None

    @pytest.mark.asyncio
    async def test_expired_draft_returns_none(self, store):
        """Expired drafts are treated as already-resolved — on_approval is a no-op."""
        draft = _make_draft(status="expired")
        store.create(draft)

        result = await on_approval(draft.draft_id, "approved", store)

        assert result is None

    @pytest.mark.asyncio
    async def test_stale_draft_cleanup_not_called_when_already_resolved(self, store):
        """Cleanup callback must NOT fire for already-resolved native drafts."""
        draft = _make_draft(draft_type="native", external_id="stale-ext", status="expired")
        store.create(draft)

        cleanup = AsyncMock()
        result = await on_approval(draft.draft_id, "discarded", store, cleanup_callback=cleanup)

        assert result is None
        cleanup.assert_not_awaited()
