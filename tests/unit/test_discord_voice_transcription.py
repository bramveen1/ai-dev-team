"""Regression tests for Discord voice/audio message ingest (issue #835).

Discord voice messages arrive as empty ``content`` + one ``audio/ogg``
attachment. Before the fix, ``_handle_inbound`` bailed on the empty-content
guard before the attachment ever reached the shared transcription pipeline
(``router.attachments.ingest_files`` -> Whisper), and logged a misleading
"MESSAGE CONTENT intent may be disabled" error along the way. These tests
pin the guard to require *both* empty content *and* no attachments before
bailing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_client() -> MagicMock:
    client = MagicMock()
    client.user = MagicMock(id=999_000)
    client.event = lambda fn: fn
    client.get_channel = MagicMock(return_value=None)
    client.get_user = MagicMock(return_value=None)
    return client


def _make_adapter(agent_name: str = "sam"):
    from router.chat.adapters.discord import DiscordAdapter

    return DiscordAdapter(
        bot_token="fake-token",
        agent_name=agent_name,
        default_channel_id=0,
        client=_make_client(),
    )


def _make_attachment(att_id: int = 1) -> MagicMock:
    attachment = MagicMock()
    attachment.id = att_id
    attachment.filename = "voice-message.ogg"
    attachment.size = 12345
    attachment.content_type = "audio/ogg"
    attachment.url = "https://cdn.discordapp.com/attachments/1/2/voice-message.ogg"
    return attachment


def _make_message(*, content: str, attachments: list | None, message_id: int = 555):
    """Return a mock ``discord.Message`` for a DM (no guild, flat channel)."""
    message = MagicMock()
    message.author = MagicMock(id=111_222, bot=False)
    message.content = content
    message.attachments = attachments or []
    message.mentions = []
    message.guild = None
    message.id = message_id
    channel = MagicMock()
    channel.id = 42
    message.channel = channel
    return message


class _StubThreadStore:
    def get_active_agent(self, channel_id, thread_ts):
        return None

    def set_active_agent(self, channel_id, thread_ts, agent_name, mentioned=False, now=None):
        pass


@pytest.fixture(autouse=True)
def thread_store(monkeypatch):
    store = _StubThreadStore()
    monkeypatch.setattr("router.chat.adapters.discord.get_default_store", lambda: store)
    return store


class TestDiscordVoiceTranscription:
    @pytest.mark.asyncio
    async def test_discord_voice_message_reaches_shared_ingest(self, monkeypatch):
        """Empty content + one audio attachment must not bail before ingest."""
        adapter = _make_adapter()
        message = _make_message(content="", attachments=[_make_attachment()])

        monkeypatch.setattr("router.chat.adapters.discord.attachments_enabled", lambda: True)
        ingest_mock = AsyncMock(return_value=(["/tmp/voice-message.txt"], True))
        adapter._ingest_attachments = ingest_mock

        with patch("router.chat.adapters.discord.handle_inbound", new_callable=AsyncMock) as hi:
            await adapter._handle_inbound(message)

        hi.assert_awaited_once()
        _, kwargs = hi.await_args
        facts_arg = hi.await_args.args[1]
        assert facts_arg.text == ""
        ingest_callback = kwargs["ingest"]
        assert ingest_callback is not None

        paths, ok = await ingest_callback()
        assert ok is True
        assert paths == ["/tmp/voice-message.txt"]
        ingest_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_intent_error_suppressed_when_attachments_present(self, monkeypatch):
        """The MESSAGE CONTENT intent warning must not fire for voice messages."""
        adapter = _make_adapter()
        message = _make_message(content="", attachments=[_make_attachment()])

        monkeypatch.setattr("router.chat.adapters.discord.attachments_enabled", lambda: True)
        adapter._ingest_attachments = AsyncMock(return_value=([], True))

        with (
            patch("router.chat.adapters.discord.handle_inbound", new_callable=AsyncMock),
            patch("router.chat.adapters.discord.logger") as mock_logger,
        ):
            await adapter._handle_inbound(message)

        mock_logger.error.assert_not_called()
        assert adapter._intent_guard_passed is None

    @pytest.mark.asyncio
    async def test_empty_message_still_bails_and_warns(self, monkeypatch):
        """No content, no attachments: still an early return with one intent warning."""
        adapter = _make_adapter()
        message = _make_message(content="", attachments=None)

        with (
            patch("router.chat.adapters.discord.handle_inbound", new_callable=AsyncMock) as hi,
            patch("router.chat.adapters.discord.logger") as mock_logger,
        ):
            await adapter._handle_inbound(message)

        hi.assert_not_called()
        mock_logger.error.assert_called_once()
        assert adapter._intent_guard_passed is False
