"""Tests for the draft state store — CRUD operations and status transitions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from router.approvals.store import Draft, DraftStore


def _make_draft(**overrides) -> Draft:
    """Create a Draft with sensible defaults, overriding any fields."""
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
    """Create a DraftStore backed by a temporary SQLite database."""
    db_path = str(tmp_path / "test_drafts.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.mark.unit
class TestDraftStoreCreate:
    def test_create_and_get(self, store):
        draft = _make_draft()
        store.create(draft)

        result = store.get(draft.draft_id)
        assert result is not None
        assert result.draft_id == draft.draft_id
        assert result.agent_name == "lisa"
        assert result.capability_type == "email"
        assert result.capability_instance == "mine"
        assert result.action_verb == "send"
        assert result.payload == {"to": "user@example.com", "subject": "Hello", "body": "Hi there!"}
        assert result.slack_channel == "C12345"
        assert result.status == "pending"
        assert result.resolved_at is None

    def test_create_duplicate_raises(self, store):
        draft = _make_draft()
        store.create(draft)
        with pytest.raises(Exception):
            store.create(draft)

    def test_payload_roundtrips_as_json(self, store):
        payload = {"nested": {"key": [1, 2, 3]}, "unicode": "café"}
        draft = _make_draft(payload=payload)
        store.create(draft)

        result = store.get(draft.draft_id)
        assert result.payload == payload


@pytest.mark.unit
class TestDraftStoreGet:
    def test_get_nonexistent_returns_none(self, store):
        assert store.get("nonexistent-id") is None

    def test_get_by_channel_ts(self, store):
        draft = _make_draft(slack_channel="C999", slack_message_ts="1705700099.000100")
        store.create(draft)

        result = store.get_by_channel_ts("C999", "1705700099.000100")
        assert result is not None
        assert result.draft_id == draft.draft_id

    def test_get_by_channel_ts_not_found(self, store):
        assert store.get_by_channel_ts("C999", "nope") is None


@pytest.mark.unit
class TestDraftStoreList:
    def test_list_by_status(self, store):
        d1 = _make_draft(status="pending")
        d2 = _make_draft(status="pending")
        store.create(d1)
        store.create(d2)

        results = store.list_by_status("pending")
        assert len(results) == 2
        ids = {r.draft_id for r in results}
        assert d1.draft_id in ids
        assert d2.draft_id in ids

    def test_list_by_status_empty(self, store):
        assert store.list_by_status("approved") == []


@pytest.mark.unit
class TestDraftStoreTransition:
    def test_transition_pending_to_approved(self, store):
        draft = _make_draft()
        store.create(draft)

        result = store.transition(draft.draft_id, "approved")
        assert result.status == "approved"
        assert result.resolved_at is not None

        # Verify persistence
        reloaded = store.get(draft.draft_id)
        assert reloaded.status == "approved"
        assert reloaded.resolved_at is not None

    def test_transition_pending_to_discarded(self, store):
        draft = _make_draft()
        store.create(draft)

        result = store.transition(draft.draft_id, "discarded")
        assert result.status == "discarded"
        assert result.resolved_at is not None

    def test_transition_pending_to_expired(self, store):
        draft = _make_draft()
        store.create(draft)

        result = store.transition(draft.draft_id, "expired")
        assert result.status == "expired"

    def test_invalid_transition_raises(self, store):
        draft = _make_draft()
        store.create(draft)
        store.transition(draft.draft_id, "approved")

        with pytest.raises(ValueError, match="Cannot transition"):
            store.transition(draft.draft_id, "discarded")

    def test_transition_nonexistent_raises(self, store):
        with pytest.raises(KeyError, match="not found"):
            store.transition("nonexistent-id", "approved")

    def test_transition_pending_to_pending_raises(self, store):
        draft = _make_draft()
        store.create(draft)
        with pytest.raises(ValueError, match="Cannot transition"):
            store.transition(draft.draft_id, "pending")


@pytest.mark.unit
class TestDraftStoreDelete:
    def test_delete_existing(self, store):
        draft = _make_draft()
        store.create(draft)

        assert store.delete(draft.draft_id) is True
        assert store.get(draft.draft_id) is None

    def test_delete_nonexistent(self, store):
        assert store.delete("nonexistent-id") is False


@pytest.mark.unit
class TestDraftStoreExternalId:
    """Tests for the external_id field used to track M365 draft IDs."""

    def test_create_with_external_id(self, store):
        draft = _make_draft(
            draft_type="native",
            external_id="AAMkAGI2TG93AAA=",
        )
        store.create(draft)

        result = store.get(draft.draft_id)
        assert result.external_id == "AAMkAGI2TG93AAA="
        assert result.draft_type == "native"

    def test_create_without_external_id(self, store):
        draft = _make_draft()
        store.create(draft)

        result = store.get(draft.draft_id)
        assert result.external_id is None

    def test_external_id_roundtrips(self, tmp_path):
        """external_id survives database reconnection."""
        db_path = str(tmp_path / "external_id_test.db")

        store1 = DraftStore(db_path)
        draft = _make_draft(external_id="graph-msg-id-123", draft_type="native")
        store1.create(draft)
        store1.close()

        store2 = DraftStore(db_path)
        result = store2.get(draft.draft_id)
        assert result.external_id == "graph-msg-id-123"
        store2.close()


@pytest.mark.unit
class TestDraftStorePersistence:
    def test_data_survives_reconnection(self, tmp_path):
        db_path = str(tmp_path / "persist_test.db")

        # Create and populate
        store1 = DraftStore(db_path)
        draft = _make_draft()
        store1.create(draft)
        store1.close()

        # Reopen and verify
        store2 = DraftStore(db_path)
        result = store2.get(draft.draft_id)
        assert result is not None
        assert result.draft_id == draft.draft_id
        assert result.agent_name == "lisa"
        store2.close()
