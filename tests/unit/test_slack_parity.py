"""Slack outbound parity harness — golden snapshots for the top ~10 message shapes (#786).

Safety net for the pre-work of #553 (slack_sdk-out-of-core / outbound-migration /
``SLACK_VIA_ADAPTER`` default flip): captures the exact kwargs a mocked Slack
``AsyncWebClient`` receives (``chat_postMessage`` / ``chat_update``) — or, for the
``/tasks`` slash-command shapes, the kwargs handed to Bolt's ``respond`` callable —
for the ten message shapes below, and asserts them against committed golden JSON
fixtures under ``tests/unit/fixtures/slack_parity/``.

Every shape is captured by driving the *real* render path (block_kit builders,
``md_to_slack``, ``outbound_mention_ids``) through the real caller (an existing
handler / adapter / worker function) with a mocked Slack client — never by
hand-authoring the golden JSON. Two shapes (1, 6) have no direct prod call site
that both invokes the exact function referenced by the issue *and* itself calls
the Slack client, so the test performs the one real-caller-shaped
``chat_postMessage`` call itself, immediately after the real render call; this is
noted in each capture function's docstring.

Regenerating goldens (e.g. after an intentional rendering change):

    REGEN=1 .venv/bin/pytest tests/unit/test_slack_parity.py -m unit

This overwrites every fixture under ``tests/unit/fixtures/slack_parity/`` with
freshly captured output and skips the assertions for that run — review the git
diff before committing regenerated goldens.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "slack_parity"
REGEN = os.environ.get("REGEN") == "1"

FAKE_AGENT_MAP = {
    "sam": {"container": "sam", "name": "Sam"},
    "lisa": {"container": "lisa", "name": "Lisa"},
}


# ---------------------------------------------------------------------------
# Golden capture/compare plumbing
# ---------------------------------------------------------------------------


def _assert_matches_golden(name: str, captured: Any) -> None:
    path = FIXTURES_DIR / f"{name}.json"
    serialized = json.dumps(captured, indent=2, sort_keys=True, default=str) + "\n"
    if REGEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized)
        pytest.skip(f"REGEN=1: wrote golden {path}")
    assert path.exists(), f"missing golden fixture {path} — run with REGEN=1 to create it"
    expected = json.loads(path.read_text())
    assert captured == expected


def _make_slack_client() -> MagicMock:
    """A mock Bolt ``AsyncWebClient`` — mirrors ``tests/conftest.py::mock_slack_client``."""
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1700000000.000100"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.assistant_threads_setStatus = AsyncMock(return_value={"ok": True})
    return client


def _capture_post(call) -> dict:
    kwargs = call.kwargs
    return {
        "kind": "chat_postMessage",
        "channel": kwargs.get("channel"),
        "thread_ts": kwargs.get("thread_ts"),
        "text": kwargs.get("text"),
        "blocks": kwargs.get("blocks"),
        "metadata": kwargs.get("metadata"),
    }


def _capture_update(call) -> dict:
    kwargs = call.kwargs
    return {
        "kind": "chat_update",
        "channel": kwargs.get("channel"),
        "ts": kwargs.get("ts"),
        "text": kwargs.get("text"),
        "blocks": kwargs.get("blocks"),
    }


def _capture_respond(call) -> dict:
    kwargs = call.kwargs
    return {
        "kind": "respond",
        "text": kwargs.get("text"),
        "blocks": kwargs.get("blocks"),
    }


class _StubThreadStore:
    """In-memory ThreadStateStore stand-in — mirrors test_discord_parity.py's."""

    def __init__(self):
        self.records: dict[tuple[str, str], str] = {}

    def get_active_agent(self, channel_id, thread_ts):
        return self.records.get((channel_id, thread_ts))

    def set_active_agent(self, channel_id, thread_ts, agent_name, mentioned=False, now=None):
        self.records[(channel_id, thread_ts)] = agent_name


def _draft_store():
    from router.approvals.store import DraftStore

    return DraftStore(":memory:")


def _task_store():
    from router.scheduled_tasks.store import ScheduledTaskStore

    return ScheduledTaskStore(":memory:")


def _make_draft(**overrides):
    from router.approvals.store import Draft

    defaults = dict(
        draft_id="draft-0001",
        agent_name="lisa",
        capability_type="email",
        capability_instance="mine",
        action_verb="send",
        payload={"to": "user@example.com", "subject": "Re: contract", "body": "Please find the signed contract."},
        slack_channel="C_APPROVALS",
        slack_message_ts="",
        status="pending",
        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Draft(**defaults)


# ---------------------------------------------------------------------------
# Shape 1 — Approval card (Block Kit) — router/approvals/block_kit.py:141
# ---------------------------------------------------------------------------


async def _capture_approval_card() -> dict:
    """Drive the real card-posting path: router.internal_api._build_and_post_card.

    This is the actual prod call site that renders the draft via
    ``SlackApprovalAdapter`` (which wraps ``build_approval_message_from_specs``)
    and posts it — the only patch is ``discover_packs`` (no packs directory in
    this test process), so button resolution falls through to the real
    discard-only fallback in ``resolve_buttons``.
    """
    from router.internal_api import _build_and_post_card

    draft = _make_draft(draft_id="draft-card", slack_channel="C_APPROVALS")
    client = _make_slack_client()
    with patch("router.internal_api.discover_packs", return_value={}):
        await _build_and_post_card(draft=draft, thread_ts="1700000000.000100", client=client)
    return _capture_post(client.chat_postMessage.call_args)


# ---------------------------------------------------------------------------
# Shape 2 — Approval outcome / chat_update — router/approvals/block_kit.py:197
# ---------------------------------------------------------------------------


async def _capture_approval_outcome() -> dict:
    """Drive the real discard path: router.approvals.handlers._handle_discard.

    Discard (rather than approve) keeps this golden deterministic: the approve
    outcome text embeds a wall-clock ``resolved_at`` timestamp
    (``build_outcome_message``'s "Approved at %I:%M %p"), while discard's
    ":x: Discarded" text carries no clock-dependent content.
    """
    from router.approvals.handlers import _handle_discard, register_handlers

    store = _draft_store()
    mock_app = MagicMock()
    mock_app.action = MagicMock(return_value=lambda f: f)
    register_handlers(mock_app, store)

    draft = _make_draft(draft_id="draft-outcome", slack_channel="C_APPROVALS", slack_message_ts="1700000000.000200")
    store.create(draft)

    client = _make_slack_client()
    ack = AsyncMock()
    body = {
        "actions": [{"action_id": "discard", "value": draft.draft_id}],
        "channel": {"id": draft.slack_channel},
        "message": {"ts": draft.slack_message_ts},
        "user": {"id": "U_APPROVER"},
    }
    await _handle_discard(ack, body, client)
    return _capture_update(client.chat_update.call_args)


# ---------------------------------------------------------------------------
# Shape 3 — Task list (chunked >50 blocks) — router/scheduled_tasks/block_kit.py:59
# ---------------------------------------------------------------------------


async def _capture_task_list_chunked() -> list[dict]:
    """Drive router.scheduled_tasks.handlers._handle_list with 30 tasks.

    30 tasks × 2 blocks each + 1 header block forces the ``respond`` chunker to
    split across two Slack messages (block cap = 50). All tasks use a cron
    schedule (not one-shot) so ``_format_task_line`` never calls the
    wall-clock-dependent ``_format_fires_in`` helper — the golden stays
    deterministic.
    """
    from router.scheduled_tasks.handlers import _handle_list
    from router.scheduled_tasks.store import ScheduledTask

    store = _task_store()
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    for i in range(30):
        store.create(
            ScheduledTask(
                task_id=f"task-{i:02d}",
                agent_name="lisa",
                name=f"Recurring task {i:02d}",
                prompt=f"Do the thing #{i:02d}.",
                schedule_cron="0 9 * * 1-5",
                next_run_at=base + timedelta(minutes=i),
                destination="C_TASKS",
                created_at=base,
            )
        )

    respond = AsyncMock()
    await _handle_list("lisa", store, respond)
    return [_capture_respond(c) for c in respond.call_args_list]


# ---------------------------------------------------------------------------
# Shape 4 — Task detail — router/scheduled_tasks/block_kit.py:111
# ---------------------------------------------------------------------------


async def _capture_task_detail() -> dict:
    from router.scheduled_tasks.handlers import _handle_detail
    from router.scheduled_tasks.store import ScheduledTask

    store = _task_store()
    task = ScheduledTask(
        task_id="task-detail-01",
        agent_name="lisa",
        name="Weekly report",
        prompt="Summarize the week's PR activity across the org.",
        schedule_cron="0 9 * * 1",
        next_run_at=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
        destination="C_TASKS",
        created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
    )
    store.create(task)

    respond = AsyncMock()
    await _handle_detail("lisa", [task.task_id], store, respond)
    return _capture_respond(respond.call_args)


# ---------------------------------------------------------------------------
# Shape 5 — Agent reply (md→mrkdwn + mentions) — router/chat/adapters/slack.py:136
# ---------------------------------------------------------------------------


async def _capture_agent_reply() -> dict:
    """Drive the real SlackAdapter.send_message — bold/link/code + @mention combo (AC-3)."""
    from router.chat.adapters.slack import SlackAdapter, make_inbound_ref
    from router.chat.types import OutboundMessage

    client = _make_slack_client()
    adapter = SlackAdapter("lisa", client)
    text = "**Done.** See the [PR](https://github.com/org/repo/pull/42) — cc @lisa for `follow-up`."
    with patch(
        "router.chat.adapters.slack.outbound_mention_ids",
        new=AsyncMock(return_value={"lisa": "U_LISA"}),
    ):
        await adapter.send_message(
            OutboundMessage(text=text, conversation_ref=make_inbound_ref("C_GENERAL", "1700000000.000300"))
        )
    return _capture_post(client.chat_postMessage.call_args)


# ---------------------------------------------------------------------------
# Shape 6 — Classified error + correlation id — router/error_classifier.py:64
# ---------------------------------------------------------------------------


async def _capture_classified_error() -> dict:
    """Drive router.chat.core.run_agent_turn to a real DispatchError, then the
    same real-adapter reply the orchestrator (handle_inbound) sends on failure.

    ``run_agent_turn`` itself has no Slack call site — its caller
    (``handle_inbound``) classifies the error and posts via
    ``adapter.send_message``, so this mirrors that exact two-step sequence with
    the real ``SlackAdapter`` rather than reimplementing handle_inbound's full
    session/history machinery.
    """
    from router.chat.adapters.slack import SlackAdapter, make_inbound_ref
    from router.chat.core import run_agent_turn
    from router.chat.types import InboundMessage, OutboundMessage, PrincipalRef
    from router.dispatcher import DispatchError
    from router.error_classifier import build_error_message

    client = _make_slack_client()
    adapter = SlackAdapter("sam", client)
    ref = make_inbound_ref("C_ERRORS", "1700000000.000400")
    inbound = InboundMessage(conversation_ref=ref, principal_ref=PrincipalRef("U1"), text="please deploy")

    guard = MagicMock()
    guard.is_halted.return_value = False
    guard.record_turn.return_value = None

    with (
        patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
        patch("router.chat.core.load_agent_memory", return_value={"org_memory": "", "agent_memory": ""}),
        patch(
            "router.chat.core._run_in_container",
            new_callable=AsyncMock,
            side_effect=DispatchError("container failed"),
        ),
    ):
        try:
            await run_agent_turn(adapter, inbound, agent_name="sam", guard=guard)
        except DispatchError as exc:
            _category, user_msg = build_error_message(exc, "deadbeef")
            await adapter.send_message(OutboundMessage(text=user_msg, conversation_ref=ref))

    return _capture_post(client.chat_postMessage.call_args)


# ---------------------------------------------------------------------------
# Shape 7 — Dispatch/scheduled-task result — router/scheduled_tasks/scheduler.py:319
# AC-5: proactive path — task.destination is None, falls back to OPERATOR_DM_CHANNEL.
# ---------------------------------------------------------------------------


async def _capture_scheduled_result_default_channel() -> dict:
    from router.scheduled_tasks.scheduler import run_task
    from router.scheduled_tasks.store import ScheduledTask

    store = _task_store()
    task = ScheduledTask(
        task_id="task-nightly-sweep",
        agent_name="lisa",
        name="Nightly inbox sweep",
        prompt="Summarize overnight inbox activity.",
        schedule_cron="0 6 * * *",
        next_run_at=datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc),
        destination=None,
        created_at=datetime(2026, 2, 28, 6, 0, tzinfo=timezone.utc),
    )
    store.create(task)

    client = _make_slack_client()

    async def dispatch_fn(*, agent_name, message, channel, thread_ts, client, timeout):
        return {"response": "Inbox is quiet — nothing urgent overnight."}

    with patch(
        "router.scheduled_tasks.scheduler.settings.get",
        side_effect=lambda key: "C_OPERATOR_DM" if key == "OPERATOR_DM_CHANNEL" else None,
    ):
        await run_task(
            task,
            store,
            lambda _agent: client,
            dispatch_fn,
            now=datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc),
        )

    return _capture_post(client.chat_postMessage.call_args)


# ---------------------------------------------------------------------------
# Shape 8 — Session-end notice — router/session_end.py:368
# ---------------------------------------------------------------------------


async def _capture_session_end_notice() -> dict:
    from router import session_end

    client = _make_slack_client()
    summary_data = {
        "topic": "auth review",
        "key_points": "found bugs",
        "open_question": "rate limiting",
        "pending_action": "PR review",
    }
    memory_data = {"agent_memory": "reviewed auth"}
    call_count = 0

    async def mock_invoke(container, prompt, timeout=60):
        nonlocal call_count
        call_count += 1
        return summary_data if call_count == 1 else memory_data

    with (
        patch("router.session_end._invoke_cli_for_extraction", side_effect=mock_invoke),
        patch("router.session_end.persist_memory", new_callable=AsyncMock, return_value=1),
    ):
        await session_end.handle_timeout_exit(
            agent_name="lisa",
            container="lisa",
            thread_history=[{"user": "U001", "text": "let's dig into the auth flow"}],
            slack_client=client,
            channel="C_SESSION",
            thread_ts="1700000000.000500",
        )

    return _capture_post(client.chat_postMessage.call_args)


# ---------------------------------------------------------------------------
# Shape 9 — Attachment conversion warning — router/slack_events.py:459
# ---------------------------------------------------------------------------


_ATTACHMENT_WARNING_TEXT = "Couldn't convert `report.docx`, please paste the content or attach as PDF/text."


async def _capture_attachment_warning() -> dict:
    """Drive the real inbound pipeline: router.slack_events._handle_event.

    ``ingest_files``/``validate_files`` are patched (no real download/convert)
    but ``inbound_common.ingest_attachments`` — the shared validate→ingest→warn
    contract that turns a conversion warning into a ``:warning:``-prefixed
    ``_post_attachment_notice`` call — runs for real.
    """
    from router import settings as settings_mod
    from router import slack_events

    _orig_settings_get = settings_mod.get
    # Event dedup is a module-level cache keyed on (channel, user, ts); clear it
    # so re-invoking this capture function (anchor test + detail test) doesn't
    # get silently dropped as a duplicate of the first call.
    slack_events._seen_events.clear()

    client = _make_slack_client()
    say = AsyncMock()
    event = {
        "type": "message",
        "user": "U_HUMAN",
        "text": "please review this doc",
        "channel": "C_ATTACH",
        "ts": "1700000000.000600",
        "thread_ts": "1700000000.000600",
        "files": [
            {
                "id": "F_DOCX",
                "name": "report.docx",
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size": 2048,
                "url_private": "https://files.slack.com/files/F_DOCX",
                "title": "report.docx",
            }
        ],
    }

    async def fake_dispatch(*, agent_name, message, **kw):
        return {"response": "noted, thanks"}

    store = _StubThreadStore()

    with (
        patch("router.slack_events.get_agent_map", return_value={"sam": {"container": "sam", "name": "Sam"}}),
        patch(
            "router.slack_events.config",
            {"slack_credentials": {"sam": {"bot_token": "xoxb-sam"}}, "session_timeout": 60},
        ),
        patch("router.slack_events.get_default_store", return_value=store),
        patch("router.slack_events.find_session_by_thread", return_value=None),
        patch(
            "router.slack_events.create_session",
            return_value={"session_id": "s1", "agent_name": "sam", "thread_history": []},
        ),
        patch("router.slack_events.update_activity"),
        patch("router.slack_events.add_to_thread_history"),
        patch("router.slack_events.dispatch", side_effect=fake_dispatch),
        patch("router.slack_events.md_to_slack", side_effect=lambda t, *_a, **_k: t),
        patch("router.slack_events.attachments_enabled", return_value=True),
        patch("router.slack_events.validate_files", side_effect=lambda files, *_a, **_k: (files, None)),
        patch(
            "router.slack_events.ingest_files",
            new_callable=AsyncMock,
            return_value=([], [_ATTACHMENT_WARNING_TEXT]),
        ),
        patch("router.slack_events.is_exit_trigger", return_value=False),
        patch("router.slack_events.needs_curation", return_value=False),
        patch(
            "router.slack_events.settings.get",
            side_effect=lambda key: False if key == "SLACK_VIA_ADAPTER" else _orig_settings_get(key),
        ),
    ):
        await slack_events._handle_event(event, say, client, receiving_agent="sam", was_mentioned=True)

    warning_calls = [c for c in client.chat_postMessage.call_args_list if ":warning:" in (c.kwargs.get("text") or "")]
    assert warning_calls, "expected a conversion-warning chat_postMessage call"
    return _capture_post(warning_calls[0])


# ---------------------------------------------------------------------------
# Shape 10 — Approval expiry banner — router/approvals/expiration_worker.py:_expire_draft
# ---------------------------------------------------------------------------


async def _capture_approval_expiry_banner() -> dict:
    """Drive router.approvals.expiration_worker.run_once past a draft's expires_at.

    The issue points at line 86 (``_send_reminder``, text-only), but AC-3 calls
    shape 10 a Block-Kit shape asserting "full blocks structure" — that is
    ``_expire_draft``'s ``chat_update`` (~line 104), the actual expiry banner.
    ``now`` is set past both the reminder and expiry thresholds; the reminder
    fires too (a ``chat_postMessage``, ignored here) but only the expiry
    ``chat_update`` is captured for this golden.
    """
    from router.approvals.expiration_worker import run_once
    from router.approvals.store import Draft

    store = _draft_store()
    t0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    draft = Draft(
        draft_id="draft-expiring",
        agent_name="lisa",
        capability_type="email",
        capability_instance="mine",
        action_verb="send",
        payload={"to": "user@example.com", "subject": "Quarterly update"},
        slack_channel="C_APPROVALS",
        slack_message_ts="1700000000.000700",
        status="pending",
        created_at=t0,
        expires_at=t0 + timedelta(hours=24),
    )
    store.create(draft)

    client = _make_slack_client()
    now = t0 + timedelta(hours=25)
    await run_once(store, client, now=now, ttl_config={"default": "24h", "reminder_ratio": 0.5, "cleanup_days": 7})
    return _capture_update(client.chat_update.call_args)


# ---------------------------------------------------------------------------
# Anchor test (AC-7) — parametrized over all 10 shapes
# ---------------------------------------------------------------------------

_SHAPES: dict[str, Callable[[], Awaitable[Any]]] = {
    "01_approval_card": _capture_approval_card,
    "02_approval_outcome": _capture_approval_outcome,
    "03_task_list_chunked": _capture_task_list_chunked,
    "04_task_detail": _capture_task_detail,
    "05_agent_reply": _capture_agent_reply,
    "06_classified_error": _capture_classified_error,
    "07_scheduled_result_default_channel": _capture_scheduled_result_default_channel,
    "08_session_end_notice": _capture_session_end_notice,
    "09_attachment_conversion_warning": _capture_attachment_warning,
    "10_approval_expiry_banner": _capture_approval_expiry_banner,
}


class TestSlackGoldenParity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape_id", sorted(_SHAPES))
    async def test_all_shapes_match_golden(self, shape_id):
        captured = await _SHAPES[shape_id]()
        _assert_matches_golden(shape_id, captured)


# ---------------------------------------------------------------------------
# Per-shape detail assertions (AC-3, AC-5)
# ---------------------------------------------------------------------------


class TestBlockKitShapes:
    """Shapes 1-4 and 10 assert the full blocks structure."""

    @pytest.mark.asyncio
    async def test_approval_card_blocks_structure(self):
        captured = await _capture_approval_card()
        block_types = [b["type"] for b in captured["blocks"]]
        assert block_types[0] == "header"
        assert "actions" in block_types
        actions_block = next(b for b in captured["blocks"] if b["type"] == "actions")
        assert actions_block["elements"][0]["action_id"] == "discard"

    @pytest.mark.asyncio
    async def test_approval_outcome_blocks_structure(self):
        captured = await _capture_approval_outcome()
        assert captured["kind"] == "chat_update"
        section = next(b for b in captured["blocks"] if b["type"] == "section")
        assert "Discarded" in section["text"]["text"]

    @pytest.mark.asyncio
    async def test_task_list_chunked_into_two_messages(self):
        captured = await _capture_task_list_chunked()
        assert len(captured) == 2
        for message in captured:
            assert len(message["blocks"]) <= 50
        assert captured[0]["blocks"][0]["type"] == "header"

    @pytest.mark.asyncio
    async def test_task_detail_blocks_structure(self):
        captured = await _capture_task_detail()
        block_types = [b["type"] for b in captured["blocks"]]
        assert block_types == ["section", "divider", "section"]
        assert "Weekly report" in captured["blocks"][0]["text"]["text"]

    @pytest.mark.asyncio
    async def test_approval_expiry_banner_blocks_structure(self):
        captured = await _capture_approval_expiry_banner()
        assert captured["kind"] == "chat_update"
        section = captured["blocks"][0]
        assert "expired" in section["text"]["text"].lower()


class TestTextShapes:
    """Shapes 5-9 assert text and (for shape 5) md_to_slack + mention rewriting (AC-3)."""

    @pytest.mark.asyncio
    async def test_agent_reply_markdown_and_mention_rewritten(self):
        captured = await _capture_agent_reply()
        text = captured["text"]
        assert "**" not in text  # bold converted
        assert "<https://github.com/org/repo/pull/42|PR>" in text  # link converted
        assert "`follow-up`" in text  # inline code preserved as-is
        assert "<@U_LISA>" in text  # plain-text @mention rewritten
        assert captured["blocks"] is None

    @pytest.mark.asyncio
    async def test_classified_error_carries_correlation_id(self):
        captured = await _capture_classified_error()
        assert "Worker exited with an error" in captured["text"]
        assert "`deadbeef`" in captured["text"]

    @pytest.mark.asyncio
    async def test_scheduled_result_falls_back_to_operator_dm_channel(self):
        """AC-5: conversation_ref-less proactive path uses the default-channel fallback."""
        captured = await _capture_scheduled_result_default_channel()
        assert captured["channel"] == "C_OPERATOR_DM"
        assert "Inbox is quiet" in captured["text"]

    @pytest.mark.asyncio
    async def test_session_end_notice_carries_harness_metadata(self):
        captured = await _capture_session_end_notice()
        assert "auth review" in captured["text"]
        assert captured["metadata"]["event_type"] == "harness_session_summary"

    @pytest.mark.asyncio
    async def test_attachment_conversion_warning_mentions_filename(self):
        captured = await _capture_attachment_warning()
        assert ":warning:" in captured["text"]
        assert "report.docx" in captured["text"]
