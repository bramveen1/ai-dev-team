"""Tests for Discord ↔ Slack transport parity.

Covers the inbound-pipeline stages the Slack path (``app._handle_event`` /
``app.handle_message``) has always had and the Discord adapter gained for
parity: bot-message guard + allowlist, summary-marker guard, event dedup, DM
support, active-agent thread routing, thread-state recording, pack commands,
session management, exit trigger, memory curation, attachments ingest,
classified error replies, and agent handoff detection — plus the
``run_agent_turn`` core-seam upgrades (stuck guard, pack extras, summary
resume, token budget, API-error classification, timeout notification) and the
app.py wiring (tasks_store, transport-aware session cleanup).

All discord.py network calls are mocked; no live Discord connection is made.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _StubThreadStore:
    """In-memory ThreadStateStore stand-in so tests never touch SQLite."""

    def __init__(self):
        self.records: dict[tuple[str, str], str] = {}

    def get_active_agent(self, channel_id, thread_ts):
        return self.records.get((channel_id, thread_ts))

    def set_active_agent(self, channel_id, thread_ts, agent_name, mentioned=False, now=None):
        self.records[(channel_id, thread_ts)] = agent_name


@pytest.fixture(autouse=True)
def thread_store(monkeypatch):
    store = _StubThreadStore()
    # The adapter's routing gate and core's orchestrator (handle_inbound)
    # each resolve the thread store from their own module.
    monkeypatch.setattr("router.chat.adapters.discord.get_default_store", lambda: store)
    monkeypatch.setattr("router.chat.core.get_default_store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _no_background_curation(monkeypatch):
    monkeypatch.setattr("router.chat.core.needs_curation", lambda *_a, **_k: False)


@pytest.fixture(autouse=True)
def _fresh_sessions(monkeypatch):
    """Give every test an empty in-memory session store."""
    import router.session_manager as sm

    monkeypatch.setattr(sm, "_sessions", {})


def _make_adapter_capturing(agent_name: str = "sam"):
    """Return (adapter, bot_user, on_message) with the registered handler captured."""
    from router.chat.adapters.discord import DiscordAdapter

    bot_user = MagicMock(id=999_000)
    client = MagicMock()
    client.user = bot_user
    captured = {}

    def _capture(fn):
        captured[fn.__name__] = fn
        return fn

    client.event = _capture
    client.get_channel = MagicMock(return_value=None)
    client.get_user = MagicMock(return_value=None)

    adapter = DiscordAdapter(bot_token="fake-token", agent_name=agent_name, client=client)
    return adapter, bot_user, captured["on_message"]


def _make_message(bot_user, *, mentioned: bool = True, content: str = "hey @sam help", bot_author: bool = False):
    """Return a mock discord.Message for a guild channel (not a thread)."""
    msg = MagicMock()
    msg.author = MagicMock(id=123_456, bot=bot_author)
    msg.attachments = []
    msg.mentions = [bot_user] if mentioned else []
    msg.content = content
    msg.guild = MagicMock(id=1)
    msg.channel = MagicMock(id=42)
    msg.thread = None
    mock_new_thread = MagicMock()
    mock_new_thread.id = 77777
    mock_new_thread.join = AsyncMock()
    msg.create_thread = AsyncMock(return_value=mock_new_thread)
    return msg


def _make_thread_message(bot_user, *, thread_id: int = 42, parent_id: int = 100, mentioned: bool = False):
    """Return a mock discord.Message whose channel is a thread the bot is a member of."""
    import discord as discord_lib

    msg = MagicMock()
    msg.author = MagicMock(id=123_456, bot=False)
    msg.attachments = []
    msg.mentions = [bot_user] if mentioned else []
    msg.content = "follow-up"
    msg.guild = MagicMock(id=1)
    ch = MagicMock()
    ch.id = thread_id
    ch.parent_id = parent_id
    ch.me = MagicMock()
    ch.__class__ = discord_lib.Thread
    msg.channel = ch
    msg.thread = None
    return msg


FAKE_AGENT_MAP = {
    "sam": {"container": "agent-sam", "name": "Sam"},
    "lisa": {"container": "agent-lisa", "name": "Lisa"},
}


# ---------------------------------------------------------------------------
# Bot-message guard + allowlist (Slack parity: _handle_event's bot guard)
# ---------------------------------------------------------------------------


class TestBotMessageGuard:
    @pytest.mark.asyncio
    async def test_bot_authored_message_is_dropped(self):
        """A non-allowlisted bot author never triggers an agent turn (loop guard)."""
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, bot_author=True)

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowlisted_bot_dispatches_as_non_human(self, monkeypatch):
        """Allowlisted bots pass the guard but do NOT reset the stuck-guard cap."""
        monkeypatch.setenv("DISCORD_DISPATCH_BOT_IDS", "123456")
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, bot_author=True)

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()
        assert mock_run.call_args.kwargs["human_initiated"] is False

    @pytest.mark.asyncio
    async def test_human_message_dispatches_as_human(self):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        assert mock_run.call_args.kwargs["human_initiated"] is True

    @pytest.mark.asyncio
    async def test_allowlisted_bot_summary_is_context_only(self, monkeypatch):
        """Guard 1 (#547): peer/harness summaries never create a dispatchable turn."""
        monkeypatch.setenv("DISCORD_DISPATCH_BOT_IDS", "123456")
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, bot_author=True, content="_Session paused. Here's where we left off:_")

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Event dedup (Slack parity: _seen_events)
# ---------------------------------------------------------------------------


class TestEventDedup:
    @pytest.mark.asyncio
    async def test_same_message_id_processed_once(self):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)
        msg.id = 424242

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)
            await on_message(msg)

        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_distinct_message_ids_both_processed(self):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg_a = _make_message(bot_user)
        msg_a.id = 1
        msg_b = _make_message(bot_user)
        msg_b.id = 2

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg_a)
            await on_message(msg_b)

        assert mock_run.await_count == 2


# ---------------------------------------------------------------------------
# DM support (Slack parity: channel_type == "im" branch)
# ---------------------------------------------------------------------------


class TestDirectMessages:
    @pytest.mark.asyncio
    async def test_dm_dispatches_without_mention(self):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=False, content="hello sam")
        msg.guild = None

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()
        # No thread creation in DMs.
        msg.create_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dm_ref_encodes_guild_zero(self):
        from router.chat.adapters.discord import _decode_ref

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, mentioned=False, content="hello")
        msg.guild = None

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        inbound = mock_run.call_args.args[1]
        guild_id, channel_id, _ = _decode_ref(inbound.conversation_ref)
        assert guild_id == 0
        assert channel_id == 42


# ---------------------------------------------------------------------------
# Active-agent thread routing + recording (Slack parity: handle_message rules)
# ---------------------------------------------------------------------------


class TestActiveAgentRouting:
    @pytest.mark.asyncio
    async def test_unmentioned_followup_dropped_when_other_agent_active(self, thread_store):
        thread_store.records[("100", "42")] = "lisa"
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_thread_message(bot_user)

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmentioned_followup_handled_when_self_active(self, thread_store):
        thread_store.records[("100", "42")] = "sam"
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_thread_message(bot_user)

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mention_always_wins_over_active_agent(self, thread_store):
        thread_store.records[("100", "42")] = "lisa"
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_thread_message(bot_user, mentioned=True)

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handled_message_records_active_agent(self, thread_store):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)  # root mention → opens thread 77777

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock):
            await on_message(msg)

        assert thread_store.records[("42", "77777")] == "sam"

    @pytest.mark.asyncio
    async def test_response_handoff_promotes_mentioned_agent(self, thread_store):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch(
                "router.chat.core.run_agent_turn",
                new_callable=AsyncMock,
                return_value="Good question — I'll loop in @lisa on this.",
            ),
        ):
            await on_message(msg)

        assert thread_store.records[("42", "77777")] == "lisa"

    @pytest.mark.asyncio
    async def test_response_self_mention_keeps_active_agent(self, thread_store):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch(
                "router.chat.core.run_agent_turn",
                new_callable=AsyncMock,
                return_value="@sam here, all done.",
            ),
        ):
            await on_message(msg)

        assert thread_store.records[("42", "77777")] == "sam"


# ---------------------------------------------------------------------------
# Pack command surface (Slack parity: grants + pending replies)
# ---------------------------------------------------------------------------


class TestPackCommands:
    @pytest.mark.asyncio
    async def test_token_reply_without_pending_falls_through_to_agent(self):
        """Bare token-like messages no longer consumed by a pending-reply mechanism.

        The resolve_pending_reply / maybe_handle_pack_command paths were removed from
        the Discord adapter in #690.  A bare message (no aidt prefix) now falls
        through to the agent turn.
        """
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="ghp_sometokenvalue")

        with patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run:
            await on_message(msg)

        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aidt_pack_verb_handled_via_dispatch_command(self):
        """aidt grant goes through _handle_aidt_command → dispatch_command (#690).

        The old maybe_handle_pack_command path is gone; pack verbs now route
        through the transport-neutral dispatch_command surface and return a
        CommandResult that the adapter renders.
        """
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="aidt list packs")

        from router.commands.types import CommandResult

        with (
            patch(
                "router.commands.core.dispatch_command",
                new_callable=AsyncMock,
                return_value=CommandResult(text="Available packs (0):"),
            ) as mock_dispatch,
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run,
        ):
            await on_message(msg)

        mock_dispatch.assert_awaited_once()
        mock_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Session management + exit trigger + curation (Slack parity)
# ---------------------------------------------------------------------------


class TestSessions:
    @pytest.mark.asyncio
    async def test_session_created_and_history_recorded(self):
        from router.session_manager import find_session_by_thread

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="hi sam")

        with patch(
            "router.chat.core.run_agent_turn",
            new_callable=AsyncMock,
            return_value="hello human",
        ):
            await on_message(msg)

        session = find_session_by_thread("42", "77777", agent_name="sam")
        assert session is not None
        assert session["transport"] == "discord"
        assert session["conversation_ref"].startswith("discord:1:42:77777")
        history = session["thread_history"]
        assert {"user": "123456", "text": "hi sam"} in history
        assert {"user": "sam", "text": "hello human"} in history

    @pytest.mark.asyncio
    async def test_exit_trigger_persists_memory_and_closes_session(self):
        from router.session_manager import find_session_by_thread

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="thanks")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.handle_clean_exit", new_callable=AsyncMock, return_value=2) as mock_exit,
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run,
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        mock_exit.assert_awaited_once()
        assert mock_exit.call_args.kwargs["container"] == "agent-sam"
        mock_run.assert_not_awaited()
        sent_texts = [c.args[0].text for c in mock_send.call_args_list]
        assert any("saved our conversation notes" in t for t in sent_texts)
        assert find_session_by_thread("42", "77777", agent_name="sam") is None

    @pytest.mark.asyncio
    async def test_exit_trigger_without_memories_stays_quiet(self):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="thanks")

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.handle_clean_exit", new_callable=AsyncMock, return_value=0),
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run,
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        mock_run.assert_not_awaited()
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_curation_triggered_on_first_message(self):
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)

        curate = AsyncMock()
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.needs_curation", return_value=True),
            patch("router.chat.core.is_curation_in_flight", return_value=False),
            patch("router.chat.core.curate_agent_memory", curate),
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock),
        ):
            await on_message(msg)
            await asyncio.sleep(0)  # let the background task start

        curate.assert_called_once_with("sam", "agent-sam")


# ---------------------------------------------------------------------------
# Attachments ingest (Slack parity: #328 pipeline)
# ---------------------------------------------------------------------------


def _attachment(filename="notes.txt", size=10, content_type="text/plain"):
    att = MagicMock()
    att.id = 9001
    att.filename = filename
    att.size = size
    att.content_type = content_type
    att.url = f"https://cdn.discordapp.com/attachments/1/2/{filename}"
    return att


class TestAttachments:
    @pytest.mark.asyncio
    async def test_ingested_paths_prepended_to_dispatch_text(self, monkeypatch):
        monkeypatch.setattr("router.chat.adapters.discord.attachments_enabled", lambda: True)
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user, content="summarize this")
        msg.attachments = [_attachment()]

        ingested = (["/var/lib/attachments/77777/notes.txt"], [])
        with (
            patch(
                "router.chat.adapters.discord.validate_files",
                side_effect=lambda files, *_a, **_k: (files, None),
            ) as mock_validate,
            patch(
                "router.chat.adapters.discord.ingest_files", new_callable=AsyncMock, return_value=ingested
            ) as mock_ingest,
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run,
        ):
            await on_message(msg)

        # The Slack-shaped file dict is built from the Discord attachment.
        (files, thread_key), _ = mock_validate.call_args
        assert files[0]["name"] == "notes.txt"
        assert files[0]["mimetype"] == "text/plain"
        assert files[0]["url_private"].startswith("https://cdn.discordapp.com/")
        assert thread_key == "77777"
        mock_ingest.assert_awaited_once()

        inbound = mock_run.call_args.args[1]
        assert inbound.text.startswith("[ATTACHMENTS]\n/var/lib/attachments/77777/notes.txt")
        assert "summarize this" in inbound.text
        assert inbound.attachments == ["/var/lib/attachments/77777/notes.txt"]

    @pytest.mark.asyncio
    async def test_validation_rejection_aborts_dispatch(self, monkeypatch):
        monkeypatch.setattr("router.chat.adapters.discord.attachments_enabled", lambda: True)
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)
        msg.attachments = [_attachment(size=999_999_999)]

        with (
            patch("router.chat.adapters.discord.validate_files", return_value=([], "File too large")),
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run,
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        mock_run.assert_not_awaited()
        assert "Could not ingest attachments" in mock_send.call_args.args[0].text

    @pytest.mark.asyncio
    async def test_ingest_failure_aborts_dispatch(self, monkeypatch):
        monkeypatch.setattr("router.chat.adapters.discord.attachments_enabled", lambda: True)
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)
        msg.attachments = [_attachment()]

        with (
            patch(
                "router.chat.adapters.discord.validate_files",
                side_effect=lambda files, *_a, **_k: (files, None),
            ),
            patch(
                "router.chat.adapters.discord.ingest_files",
                new_callable=AsyncMock,
                side_effect=RuntimeError("disk full"),
            ),
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock) as mock_run,
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        mock_run.assert_not_awaited()
        assert "Could not process attachments" in mock_send.call_args.args[0].text

    @pytest.mark.asyncio
    async def test_conversion_warnings_are_posted(self, monkeypatch):
        monkeypatch.setattr("router.chat.adapters.discord.attachments_enabled", lambda: True)
        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)
        msg.attachments = [_attachment("report.docx", content_type="application/msword")]

        ingested = ([], ["Couldn't convert `report.docx`, please paste the content or attach as PDF/text."])
        with (
            patch(
                "router.chat.adapters.discord.validate_files",
                side_effect=lambda files, *_a, **_k: (files, None),
            ),
            patch("router.chat.adapters.discord.ingest_files", new_callable=AsyncMock, return_value=ingested),
            patch("router.chat.core.run_agent_turn", new_callable=AsyncMock),
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        warned = [c.args[0].text for c in mock_send.call_args_list if ":warning:" in c.args[0].text]
        assert warned and "report.docx" in warned[0]


# ---------------------------------------------------------------------------
# Classified error replies (Slack parity: error_classifier)
# ---------------------------------------------------------------------------


class TestErrorReplies:
    @pytest.mark.asyncio
    async def test_dispatch_error_reply_carries_correlation_ref(self):
        from router.dispatcher import DispatchError

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)

        with (
            patch(
                "router.chat.core.run_agent_turn",
                new_callable=AsyncMock,
                side_effect=DispatchError("container failed"),
            ),
            patch.object(adapter, "set_status", new_callable=AsyncMock),
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        text = mock_send.call_args.args[0].text
        assert "Worker exited with an error" in text
        assert "`" in text  # correlation id is quoted

    @pytest.mark.asyncio
    async def test_api_overload_reply_asks_for_retry(self):
        from router.dispatcher import ApiError

        adapter, bot_user, on_message = _make_adapter_capturing("sam")
        msg = _make_message(bot_user)

        with (
            patch(
                "router.chat.core.run_agent_turn",
                new_callable=AsyncMock,
                side_effect=ApiError(529, "overloaded"),
            ),
            patch.object(adapter, "set_status", new_callable=AsyncMock),
            patch.object(adapter, "send_message", new_callable=AsyncMock) as mock_send,
        ):
            await on_message(msg)

        assert "overloaded" in mock_send.call_args.args[0].text.lower()


# ---------------------------------------------------------------------------
# read_thread summary provenance (Guard 2, #547)
# ---------------------------------------------------------------------------


class TestReadThreadSummaryProvenance:
    async def _read(self, bot_authored: bool, text: str):
        from router.chat.adapters.discord import DiscordAdapter, make_inbound_ref
        from router.chat.types import ConversationRef

        bot_user = MagicMock(id=999_000)
        client = MagicMock()
        client.user = bot_user
        client.event = lambda fn: fn

        author = bot_user if bot_authored else MagicMock(id=555)
        mock_msg = MagicMock(content=text, author=author, guild=MagicMock(id=1))

        mock_channel = MagicMock()
        mock_channel.get_thread = MagicMock(return_value=None)

        async def _history(**kwargs):
            yield mock_msg

        mock_channel.history = MagicMock(return_value=_history())
        client.get_channel = MagicMock(return_value=mock_channel)

        adapter = DiscordAdapter(bot_token="t", agent_name="sam", client=client)
        return await adapter.read_thread(ConversationRef(make_inbound_ref(1, 42, 0)))

    @pytest.mark.asyncio
    async def test_own_summary_is_flagged(self):
        messages = await self._read(bot_authored=True, text="_Session paused. Here's where we left off:_")
        assert messages[0].is_summary is True

    @pytest.mark.asyncio
    async def test_foreign_summary_text_is_not_flagged(self):
        messages = await self._read(bot_authored=False, text="_Session paused. Here's where we left off:_")
        assert messages[0].is_summary is False

    @pytest.mark.asyncio
    async def test_own_normal_message_is_not_flagged(self):
        messages = await self._read(bot_authored=True, text="here is the answer")
        assert messages[0].is_summary is False


# ---------------------------------------------------------------------------
# run_agent_turn parity with dispatch() (core seam)
# ---------------------------------------------------------------------------


from router.chat.interface import ChatAdapter  # noqa: E402
from router.chat.types import (  # noqa: E402
    AdapterCapabilities,
    ConversationRef,
    InboundMessage,
    PrincipalRef,
    StructuredResponse,
)

_GOOD_JSON = json.dumps({"result": "Hello from Sam!"})
FAKE_MEMORY = {"org_memory": "", "agent_memory": ""}


class FakeCoreAdapter(ChatAdapter):
    """Minimal in-memory adapter for exercising run_agent_turn."""

    def __init__(self, history=None):
        self.history = history or []
        self.sent = []
        self.statuses = []

    @property
    def capabilities(self):
        return AdapterCapabilities()

    async def send_message(self, outbound):
        self.sent.append(outbound)

    async def read_thread(self, conversation_ref):
        return list(self.history)

    async def set_status(self, conversation_ref, state):
        self.statuses.append(state)

    def resolve_principal(self, raw_user_id):
        return PrincipalRef(raw_user_id)

    def parse_mentions(self, text, conversation_ref):
        return []

    async def prompt_for_choice(self, conversation_ref, prompt):
        return StructuredResponse(choice=prompt.choices[0], index=0)


def _inbound(text="hi", ref="fake:conv-1"):
    return InboundMessage(
        conversation_ref=ConversationRef(ref),
        principal_ref=PrincipalRef("user-1"),
        text=text,
    )


def _quiet_guard():
    guard = MagicMock()
    guard.is_halted.return_value = False
    guard.record_turn.return_value = None
    return guard


class TestRunAgentTurnStuckGuard:
    @pytest.mark.asyncio
    async def test_halted_task_raises_task_halted_error(self):
        from router.chat.core import run_agent_turn
        from router.dispatcher import TaskHaltedError

        guard = MagicMock()
        guard.is_halted.return_value = True
        guard.get_state.return_value = None

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            with pytest.raises(TaskHaltedError):
                await run_agent_turn(
                    FakeCoreAdapter(), _inbound(), agent_name="sam", human_initiated=False, guard=guard
                )

        mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_human_initiated_resets_turn_cap(self):
        from router.chat.core import run_agent_turn

        guard = _quiet_guard()
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock, return_value=(_GOOD_JSON, "", 0)),
        ):
            await run_agent_turn(FakeCoreAdapter(), _inbound(), agent_name="sam", human_initiated=True, guard=guard)

        guard.record_human_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_bot_initiated_does_not_reset_turn_cap(self):
        from router.chat.core import run_agent_turn

        guard = _quiet_guard()
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock, return_value=(_GOOD_JSON, "", 0)),
        ):
            await run_agent_turn(FakeCoreAdapter(), _inbound(), agent_name="sam", human_initiated=False, guard=guard)

        guard.record_human_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_turn_recorded(self):
        from router.chat.core import run_agent_turn

        guard = _quiet_guard()
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock, return_value=(_GOOD_JSON, "", 0)),
        ):
            await run_agent_turn(FakeCoreAdapter(), _inbound(), agent_name="sam", guard=guard)

        guard.record_turn.assert_called_once()
        assert guard.record_turn.call_args.kwargs["error_class"] is None


class TestRunAgentTurnPackExtras:
    @pytest.mark.asyncio
    async def test_pack_extras_injected_into_cli_and_env(self):
        from router.chat.core import run_agent_turn
        from router.packs.dispatch_hook import PackDispatchExtras

        extras = PackDispatchExtras(
            prompt_files=["/config/packs/dispatch/prompt.md"],
            mcp_config_path="/tmp/mcp.json",
            env={"DISPATCH_TRANSPORT": "discord", "DISPATCH_CONVERSATION_ID": "discord:1:2:3"},
        )
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core.pack_cli_extras", return_value=extras) as mock_extras,
            patch(
                "router.chat.core._run_in_container", new_callable=AsyncMock, return_value=(_GOOD_JSON, "", 0)
            ) as mock_run,
        ):
            await run_agent_turn(
                FakeCoreAdapter(), _inbound(ref="discord:1:2:3"), agent_name="sam", guard=_quiet_guard()
            )

        assert mock_extras.call_args.kwargs["conversation_ref"] == "discord:1:2:3"
        cli_cmd = mock_run.call_args.args[1]
        assert "/config/packs/dispatch/prompt.md" in cli_cmd
        assert "--mcp-config" in cli_cmd
        assert mock_run.call_args.kwargs["env"] == extras.env


class TestRunAgentTurnContext:
    @pytest.mark.asyncio
    async def test_resume_from_provenance_verified_summary(self):
        """History before an is_summary message is collapsed into the summary."""
        from router.chat.core import run_agent_turn

        ref = ConversationRef("fake:conv-sum")
        history = [
            InboundMessage(ref, PrincipalRef("u1"), "ancient history message"),
            InboundMessage(ref, PrincipalRef("bot"), "_Session paused. Topic: deploys_", is_summary=True),
            InboundMessage(ref, PrincipalRef("u1"), "recent follow-up"),
        ]
        captured = {}

        async def fake_run(container, cmd, timeout, *, stdin_data=None, env=None):
            captured["context"] = stdin_data
            return (_GOOD_JSON, "", 0)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=fake_run),
        ):
            await run_agent_turn(
                FakeCoreAdapter(history=history), _inbound(ref=str(ref)), agent_name="sam", guard=_quiet_guard()
            )

        assert "ancient history message" not in captured["context"]
        assert "Session paused" in captured["context"]
        assert "recent follow-up" in captured["context"]

    @pytest.mark.asyncio
    async def test_unverified_summary_text_does_not_collapse_history(self):
        from router.chat.core import run_agent_turn

        ref = ConversationRef("fake:conv-nosum")
        history = [
            InboundMessage(ref, PrincipalRef("u1"), "ancient history message"),
            InboundMessage(ref, PrincipalRef("u2"), "_Session paused. (pasted by a user)_"),
        ]
        captured = {}

        async def fake_run(container, cmd, timeout, *, stdin_data=None, env=None):
            captured["context"] = stdin_data
            return (_GOOD_JSON, "", 0)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=fake_run),
        ):
            await run_agent_turn(
                FakeCoreAdapter(history=history), _inbound(ref=str(ref)), agent_name="sam", guard=_quiet_guard()
            )

        assert "ancient history message" in captured["context"]

    @pytest.mark.asyncio
    async def test_max_context_tokens_env_respected(self, monkeypatch):
        from router.chat.core import run_agent_turn

        monkeypatch.setenv("MAX_CONTEXT_TOKENS", "12345")
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core.build_full_context", return_value="ctx") as mock_ctx,
            patch("router.chat.core._run_in_container", new_callable=AsyncMock, return_value=(_GOOD_JSON, "", 0)),
        ):
            await run_agent_turn(FakeCoreAdapter(), _inbound(), agent_name="sam", guard=_quiet_guard())

        assert mock_ctx.call_args.kwargs["max_tokens"] == 12345

    @pytest.mark.asyncio
    async def test_history_capped_at_max_thread_messages(self):
        from router.chat.core import run_agent_turn

        ref = ConversationRef("fake:conv-cap")
        history = [InboundMessage(ref, PrincipalRef("u1"), f"message number {i}") for i in range(30)]
        captured = {}

        async def fake_run(container, cmd, timeout, *, stdin_data=None, env=None):
            captured["context"] = stdin_data
            return (_GOOD_JSON, "", 0)

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", side_effect=fake_run),
        ):
            await run_agent_turn(
                FakeCoreAdapter(history=history), _inbound(ref=str(ref)), agent_name="sam", guard=_quiet_guard()
            )

        assert "message number 29" in captured["context"]
        assert "message number 5" not in captured["context"]  # capped at the last 20


class TestRunAgentTurnErrors:
    @pytest.mark.asyncio
    async def test_api_error_classified_from_stderr(self):
        from router.chat.core import run_agent_turn
        from router.dispatcher import ApiError

        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch(
                "router.chat.core._run_in_container",
                new_callable=AsyncMock,
                return_value=("", "API Error: 529 upstream overloaded", 1),
            ),
        ):
            with pytest.raises(ApiError) as excinfo:
                await run_agent_turn(FakeCoreAdapter(), _inbound(), agent_name="sam", guard=_quiet_guard())

        assert excinfo.value.status_code == 529

    @pytest.mark.asyncio
    async def test_error_turn_recorded_with_error_class(self):
        from router.chat.core import run_agent_turn
        from router.dispatcher import DispatchError

        guard = _quiet_guard()
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch("router.chat.core._run_in_container", new_callable=AsyncMock, return_value=("", "boom", 3)),
        ):
            with pytest.raises(DispatchError):
                await run_agent_turn(FakeCoreAdapter(), _inbound(), agent_name="sam", guard=guard)

        assert guard.record_turn.call_args.kwargs["error_class"] == "NonZeroExit(3)"

    @pytest.mark.asyncio
    async def test_timeout_posts_alarm_clock_notification(self):
        from router.chat.core import run_agent_turn
        from router.dispatcher import DispatchTimeoutError

        adapter = FakeCoreAdapter()
        with (
            patch("router.chat.core.get_agent_map", return_value=FAKE_AGENT_MAP),
            patch("router.chat.core.load_agent_memory", return_value=FAKE_MEMORY),
            patch(
                "router.chat.core._run_in_container",
                new_callable=AsyncMock,
                side_effect=DispatchTimeoutError("timed out"),
            ),
        ):
            with pytest.raises(DispatchTimeoutError):
                await run_agent_turn(adapter, _inbound(), agent_name="sam", guard=_quiet_guard())

        assert any(":alarm_clock:" in m.text for m in adapter.sent)


# ---------------------------------------------------------------------------
# app.py wiring: tasks_store + transport-aware session cleanup
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_module(monkeypatch):
    """Import router.app with all module-level side effects mocked."""
    monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("LISA_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("LISA_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("SAM_BOT_TOKEN", "xoxb-sam-test")
    monkeypatch.setenv("SAM_APP_TOKEN", "xapp-sam-test")
    monkeypatch.setenv("SAM_SIGNING_SECRET", "sam-test-secret")
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "test-internal-token")

    with (
        patch("router.app.AsyncApp") as mock_app_cls,
        patch("router.app.AsyncSocketModeHandler"),
        patch("router.app.load_dotenv"),
    ):
        mock_bolt_app = MagicMock()
        mock_bolt_app.client = MagicMock()
        mock_app_cls.return_value = mock_bolt_app

        import importlib

        import router.app as app

        importlib.reload(app)
        yield app


class TestAppDiscordWiring:
    def test_build_discord_adapters_passes_tasks_store(self, monkeypatch, app_module):
        monkeypatch.setenv("DISCORD_ENABLED", "true")
        mock_creds = {"sam": {"bot_token": "discord-token-sam", "default_channel_id": 0}}
        tasks_store = MagicMock(name="scheduled-task-store")

        captured = {}

        def fake_adapter(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with (
            patch("router.app.load_discord_credentials", return_value=mock_creds),
            patch("router.chat.adapters.discord.DiscordAdapter", side_effect=fake_adapter),
        ):
            adapters = app_module._build_discord_adapters(tasks_store=tasks_store)

        assert len(adapters) == 1
        assert captured["tasks_store"] is tasks_store

    @pytest.mark.asyncio
    async def test_discord_session_client_posts_via_adapter(self, app_module):
        adapter = MagicMock()
        adapter.agent_name = "sam"
        adapter.send_message = AsyncMock()
        app_module._discord_adapters[:] = [adapter]

        session = {
            "agent_name": "sam",
            "transport": "discord",
            "conversation_ref": "discord:1:42:77777",
        }
        client = app_module._session_end_client(session)
        assert client is not None

        await client.chat_postMessage(channel="42", thread_ts="77777", text="summary text", metadata={})

        outbound = adapter.send_message.call_args.args[0]
        assert outbound.text == "summary text"
        assert outbound.conversation_ref == "discord:1:42:77777"

    def test_discord_session_without_adapter_returns_none(self, app_module):
        app_module._discord_adapters[:] = []
        session = {"agent_name": "sam", "transport": "discord", "conversation_ref": "discord:1:2:3"}
        assert app_module._session_end_client(session) is None

    def test_slack_session_uses_agent_client(self, app_module):
        sentinel = MagicMock()
        with patch("router.session_lifecycle._client_for_agent", return_value=sentinel):
            session = {"agent_name": "sam"}
            assert app_module._session_end_client(session) is sentinel
