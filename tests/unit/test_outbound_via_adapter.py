"""Named regression tests for #801 — outbound/proactive Slack sends via ChatAdapter.

Covers the ``SLACK_OUTBOUND_VIA_ADAPTER`` flag gate: plain-text outbound
call-sites route through ``ChatAdapter.send_message`` when on, keep calling
``chat_postMessage`` directly (byte-identical) when off, and rich sends
(Block Kit edits, ``chat_update``, the session-summary metadata marker) never
move regardless of the flag. Scoped narrowly to this file per the issue —
never run against the whole ``tests/unit/`` tree.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import session_end
from router.approvals import expiration_worker
from router.chat.adapters.slack import SlackAdapter, _decode_ref, make_outbound_ref
from router.chat.adapters.slack_outbound import send as outbound_send
from router.chat.types import OutboundMessage
from router.scheduled_tasks import scheduler
from router.scheduled_tasks.store import ScheduledTask, ScheduledTaskStore

pytestmark = pytest.mark.unit


def _make_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.2"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    return client


def _make_task(**overrides) -> ScheduledTask:
    defaults = {
        "task_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "name": "Daily inbox review",
        "prompt": "Summarize yesterday's inbox.",
        "schedule_cron": "0 9 * * 1-5",
        "next_run_at": datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc),
        "destination": "C_INBOX",
        "enabled": True,
        "created_at": datetime(2026, 4, 17, 8, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.mark.asyncio
class TestProactiveSendRoutesThroughAdapter:
    async def test_proactive_send_routes_through_adapter_when_flag_on(self, store, monkeypatch):
        """Scheduler send with the flag on hits send_message, not chat_postMessage."""
        monkeypatch.setenv("SLACK_OUTBOUND_VIA_ADAPTER", "1")
        client = _make_client()
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now - timedelta(minutes=1))
        store.create(task)

        dispatch_fn = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "Inbox summary: ..."})

        with patch("router.chat.adapters.slack.SlackAdapter.send_message", new_callable=AsyncMock) as send_message:
            await scheduler.run_once(store, lambda _agent: client, dispatch_fn, now=now)
            await scheduler.drain_agent_tasks()

        send_message.assert_awaited_once()
        client.chat_postMessage.assert_not_awaited()
        outbound = send_message.await_args.args[0]
        assert outbound.text == "Inbox summary: ..."
        assert str(outbound.conversation_ref) == "slack:C_INBOX:"


@pytest.mark.asyncio
class TestLegacyPathWhenFlagOff:
    async def test_legacy_path_used_when_flag_off(self, store, monkeypatch):
        """Flag off (default) uses legacy chat_postMessage; the adapter is never touched."""
        monkeypatch.delenv("SLACK_OUTBOUND_VIA_ADAPTER", raising=False)
        client = _make_client()
        now = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        task = _make_task(next_run_at=now - timedelta(minutes=1))
        store.create(task)

        dispatch_fn = AsyncMock(return_value={"agent": "lisa", "status": "ok", "response": "Inbox summary: ..."})

        with patch("router.chat.adapters.slack.SlackAdapter.send_message", new_callable=AsyncMock) as send_message:
            await scheduler.run_once(store, lambda _agent: client, dispatch_fn, now=now)
            await scheduler.drain_agent_tasks()

        send_message.assert_not_awaited()
        client.chat_postMessage.assert_awaited_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C_INBOX"
        assert kwargs["text"] == "Inbox summary: ..."


class TestOutboundRefRoundtrips:
    def test_outbound_ref_roundtrips_through_decode(self):
        """A constructed outbound ref decodes back to the original (channel, thread_ts)."""
        ref = make_outbound_ref("C123", "1700000000.000100")
        assert str(ref) == "slack:C123:1700000000.000100"
        assert _decode_ref(ref) == ("C123", "1700000000.000100")

    def test_outbound_ref_roundtrips_with_no_thread(self):
        ref = make_outbound_ref("C123", "")
        assert _decode_ref(ref) == ("C123", "")


@pytest.mark.asyncio
class TestFailClosed:
    async def test_missing_ref_and_default_channel_fails_closed(self, caplog):
        """No ref and no default_channel logs a warning and drops — no exception, no post."""
        client = _make_client()
        adapter = SlackAdapter("sam", client, default_channel="")

        with caplog.at_level("WARNING"):
            await adapter.send_message(OutboundMessage(text="hi"))

        client.chat_postMessage.assert_not_awaited()
        assert any("dropping send" in rec.message for rec in caplog.records)

    async def test_outbound_send_helper_fails_closed_with_no_channel(self, caplog):
        """The shared slack_outbound.send() helper degrades the same way with no channel."""
        client = _make_client()

        with caplog.at_level("WARNING"):
            await outbound_send(client, "sam", "hi")

        client.chat_postMessage.assert_not_awaited()
        assert any("dropping send" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
class TestRichSendsStayOnLegacyPath:
    async def test_rich_sends_stay_on_legacy_path(self, monkeypatch):
        """Approval-card/chat_update/metadata sites still call legacy even with the flag on."""
        monkeypatch.setenv("SLACK_OUTBOUND_VIA_ADAPTER", "1")

        # chat_update (message edit) — no update primitive on the adapter yet.
        client = _make_client()
        with patch("router.chat.adapters.slack.SlackAdapter.send_message", new_callable=AsyncMock) as send_message:
            await expiration_worker._expire_draft(client, "C1", "1.0", "social", "publish")
        client.chat_update.assert_awaited_once()
        send_message.assert_not_awaited()

        # Session-timeout summary — carries the HARNESS_SUMMARY_EVENT_TYPE metadata
        # marker (#547 Guard 2), which OutboundMessage cannot carry yet.
        summary_client = _make_client()
        with (
            patch("router.session_end._invoke_cli_for_extraction", return_value=None),
            patch("router.session_end.persist_memory", new_callable=AsyncMock, return_value=0),
            patch("router.chat.adapters.slack.SlackAdapter.send_message", new_callable=AsyncMock) as send_message2,
        ):
            await session_end.handle_timeout_exit(
                agent_name="lisa",
                container="lisa",
                thread_history=[{"user": "U001", "text": "hello"}],
                slack_client=summary_client,
                channel="C001",
                thread_ts="1.0",
            )
        summary_client.chat_postMessage.assert_awaited_once()
        assert summary_client.chat_postMessage.call_args.kwargs["metadata"]["event_type"]
        send_message2.assert_not_awaited()
