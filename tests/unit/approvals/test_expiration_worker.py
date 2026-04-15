"""Tests for the draft expiration and reminder worker.

Uses a frozen clock to test time-based transitions without real delays.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from router.approvals.expiration_worker import (
    get_reminder_offset,
    get_ttl,
    parse_duration,
    run_once,
)
from router.approvals.store import Draft, DraftStore

# Base time for all tests
T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

TTL_CONFIG = {
    "default": "24h",
    "social": "8h",
    "calendar": "72h",
    "reminder_ratio": 0.5,
    "cleanup_days": 7,
}


def _make_draft(
    created_at: datetime = T0,
    capability_type: str = "email",
    draft_type: str = "direct",
    expires_at: datetime | None = None,
    **overrides,
) -> Draft:
    """Create a Draft with sensible defaults for expiration tests."""
    if expires_at is None:
        ttl = get_ttl(capability_type, TTL_CONFIG)
        expires_at = created_at + ttl

    defaults = {
        "draft_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "capability_type": capability_type,
        "capability_instance": "mine",
        "action_verb": "send",
        "payload": {"to": "user@example.com", "subject": "Test"},
        "slack_channel": "C12345",
        "slack_message_ts": "1705700000.000100",
        "draft_type": draft_type,
        "status": "pending",
        "created_at": created_at,
        "expires_at": expires_at,
    }
    defaults.update(overrides)
    return Draft(**defaults)


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_expiration.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    client.chat_update = AsyncMock(return_value={"ok": True})
    return client


@pytest.mark.unit
class TestParseDuration:
    def test_hours(self):
        assert parse_duration("24h") == timedelta(hours=24)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_days(self):
        assert parse_duration("7d") == timedelta(days=7)

    def test_uppercase(self):
        assert parse_duration("8H") == timedelta(hours=8)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("abc")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("")


@pytest.mark.unit
class TestGetTtl:
    def test_default_ttl(self):
        assert get_ttl("email", TTL_CONFIG) == timedelta(hours=24)

    def test_social_ttl(self):
        assert get_ttl("social", TTL_CONFIG) == timedelta(hours=8)

    def test_calendar_ttl(self):
        assert get_ttl("calendar", TTL_CONFIG) == timedelta(hours=72)

    def test_unknown_type_uses_default(self):
        assert get_ttl("unknown-type", TTL_CONFIG) == timedelta(hours=24)


@pytest.mark.unit
class TestGetReminderOffset:
    def test_default_reminder_at_half_ttl(self):
        offset = get_reminder_offset("email", TTL_CONFIG)
        assert offset == timedelta(hours=12)

    def test_social_reminder(self):
        offset = get_reminder_offset("social", TTL_CONFIG)
        assert offset == timedelta(hours=4)


@pytest.mark.unit
class TestNoReminderBeforeThreshold:
    @pytest.mark.asyncio
    async def test_11h_no_reminder(self, store, mock_client):
        """Draft created at T0 — 11h later, no reminder should be sent."""
        draft = _make_draft(created_at=T0)
        store.create(draft)

        now = T0 + timedelta(hours=11)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        assert counts["reminded"] == 0
        assert counts["expired"] == 0
        mock_client.chat_postMessage.assert_not_awaited()


@pytest.mark.unit
class TestReminderSent:
    @pytest.mark.asyncio
    async def test_13h_sends_reminder(self, store, mock_client):
        """Draft created at T0 — 13h later, reminder should be sent."""
        draft = _make_draft(created_at=T0)
        store.create(draft)

        now = T0 + timedelta(hours=13)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        assert counts["reminded"] == 1
        mock_client.chat_postMessage.assert_awaited_once()

        # Verify the draft was marked as reminded
        updated = store.get(draft.draft_id)
        assert updated.reminded_at is not None

    @pytest.mark.asyncio
    async def test_reminder_is_idempotent(self, store, mock_client):
        """Running again after reminder doesn't re-send."""
        draft = _make_draft(created_at=T0)
        store.create(draft)

        now = T0 + timedelta(hours=13)

        # First run — sends reminder
        await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)
        assert mock_client.chat_postMessage.await_count == 1

        # Second run — should NOT send again
        mock_client.chat_postMessage.reset_mock()
        await run_once(store, mock_client, now=now + timedelta(hours=1), ttl_config=TTL_CONFIG)
        mock_client.chat_postMessage.assert_not_awaited()


@pytest.mark.unit
class TestExpiration:
    @pytest.mark.asyncio
    async def test_25h_expires_draft(self, store, mock_client):
        """Draft created at T0 — 25h later, should expire."""
        draft = _make_draft(created_at=T0)
        store.create(draft)

        now = T0 + timedelta(hours=25)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        assert counts["expired"] == 1
        mock_client.chat_update.assert_awaited_once()

        # Verify DB state
        updated = store.get(draft.draft_id)
        assert updated.status == "expired"
        assert updated.resolved_at is not None

    @pytest.mark.asyncio
    async def test_social_expires_faster(self, store, mock_client):
        """Social drafts expire at 8h, not 24h."""
        draft = _make_draft(created_at=T0, capability_type="social")
        store.create(draft)

        # At 9h — social should be expired
        now = T0 + timedelta(hours=9)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        assert counts["expired"] == 1

    @pytest.mark.asyncio
    async def test_calendar_expires_slower(self, store, mock_client):
        """Calendar drafts expire at 72h."""
        draft = _make_draft(created_at=T0, capability_type="calendar")
        store.create(draft)

        # At 48h — calendar should NOT be expired yet
        now = T0 + timedelta(hours=48)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)
        assert counts["expired"] == 0

        # At 73h — should be expired
        now = T0 + timedelta(hours=73)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)
        assert counts["expired"] == 1

    @pytest.mark.asyncio
    async def test_expired_message_updated(self, store, mock_client):
        """Verify the Slack message is edited to show expiry."""
        draft = _make_draft(created_at=T0)
        store.create(draft)

        now = T0 + timedelta(hours=25)
        await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        call_kwargs = mock_client.chat_update.call_args.kwargs
        assert call_kwargs["channel"] == "C12345"
        blocks_text = call_kwargs["blocks"][0]["text"]["text"]
        assert "expired" in blocks_text.lower()


@pytest.mark.unit
class TestCleanup:
    @pytest.mark.asyncio
    async def test_8d_cleanup_for_native_drafts(self, store, mock_client):
        """Expired native-app draft — 8 days later, cleanup should run."""
        draft = _make_draft(created_at=T0, draft_type="native")
        store.create(draft)

        # Expire the draft first
        expire_time = T0 + timedelta(hours=25)
        await run_once(store, mock_client, now=expire_time, ttl_config=TTL_CONFIG)
        assert store.get(draft.draft_id).status == "expired"

        # 8 days after expiration — cleanup threshold is 7 days from expires_at
        cleanup_callback = AsyncMock()
        cleanup_time = draft.expires_at + timedelta(days=8)
        counts = await run_once(
            store, mock_client, now=cleanup_time, ttl_config=TTL_CONFIG, cleanup_callback=cleanup_callback
        )

        assert counts["cleaned_up"] == 1
        cleanup_callback.assert_awaited_once()

        updated = store.get(draft.draft_id)
        assert updated.status == "cleaned_up"

    @pytest.mark.asyncio
    async def test_cleanup_not_triggered_for_direct_drafts(self, store, mock_client):
        """Direct-action drafts don't need external cleanup."""
        draft = _make_draft(created_at=T0, draft_type="direct")
        store.create(draft)

        # Expire it
        expire_time = T0 + timedelta(hours=25)
        await run_once(store, mock_client, now=expire_time, ttl_config=TTL_CONFIG)

        # Way past cleanup threshold — but draft_type is 'direct', so no cleanup
        cleanup_callback = AsyncMock()
        cleanup_time = draft.expires_at + timedelta(days=30)
        counts = await run_once(
            store, mock_client, now=cleanup_time, ttl_config=TTL_CONFIG, cleanup_callback=cleanup_callback
        )

        assert counts["cleaned_up"] == 0
        cleanup_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cleanup_callback_failure_skips_draft(self, store, mock_client):
        """If cleanup callback raises, draft stays expired (not cleaned_up)."""
        draft = _make_draft(created_at=T0, draft_type="native")
        store.create(draft)

        expire_time = T0 + timedelta(hours=25)
        await run_once(store, mock_client, now=expire_time, ttl_config=TTL_CONFIG)

        cleanup_callback = AsyncMock(side_effect=RuntimeError("Provider API down"))
        cleanup_time = draft.expires_at + timedelta(days=8)
        counts = await run_once(
            store, mock_client, now=cleanup_time, ttl_config=TTL_CONFIG, cleanup_callback=cleanup_callback
        )

        assert counts["cleaned_up"] == 0
        assert store.get(draft.draft_id).status == "expired"


@pytest.mark.unit
class TestMultipleDrafts:
    @pytest.mark.asyncio
    async def test_mixed_types_processed_correctly(self, store, mock_client):
        """Multiple drafts of different types — each uses its own TTL."""
        email_draft = _make_draft(created_at=T0, capability_type="email")
        social_draft = _make_draft(created_at=T0, capability_type="social")
        calendar_draft = _make_draft(created_at=T0, capability_type="calendar")

        store.create(email_draft)
        store.create(social_draft)
        store.create(calendar_draft)

        # At 9h: social expired (8h TTL), email & calendar pending
        now = T0 + timedelta(hours=9)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        assert counts["expired"] == 1  # social
        assert counts["reminded"] == 1  # email (reminder at 12h? no, 12h > 9h, so no reminder yet)

        # Actually: email reminder at 12h, social reminder at 4h.
        # At 9h: social would get reminder at 4h (already past) BUT it expired too.
        # Let me re-check: social TTL=8h, reminder at 4h. At 9h it's past expiry.
        # The worker processes reminders first, then expirations.
        # Social at 9h: past reminder (4h) but also past expiry (8h).
        # Phase A: social not reminded yet & past 4h → send reminder, count reminded
        # Phase B: social pending & past 8h → expire, count expired
        # So social gets BOTH reminded and expired in same run.
        # email at 9h: reminder at 12h → not yet
        # calendar at 9h: reminder at 36h → not yet

        # The social draft gets both reminded and expired
        assert store.get(social_draft.draft_id).status == "expired"
        assert store.get(email_draft.draft_id).status == "pending"
        assert store.get(calendar_draft.draft_id).status == "pending"

    @pytest.mark.asyncio
    async def test_already_approved_not_affected(self, store, mock_client):
        """Approved drafts are not reminded or expired."""
        draft = _make_draft(created_at=T0)
        store.create(draft)
        store.transition(draft.draft_id, "approved")

        now = T0 + timedelta(hours=25)
        counts = await run_once(store, mock_client, now=now, ttl_config=TTL_CONFIG)

        assert counts["reminded"] == 0
        assert counts["expired"] == 0
