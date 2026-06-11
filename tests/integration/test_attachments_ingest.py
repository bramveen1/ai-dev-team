"""Integration tests for Slack attachment ingest through _handle_event (#328, #329).

Mocks the Slack API and downloader; verifies:
  - oversize file → in-thread rejection reply, no dispatch
  - valid PDF + valid PNG → files land in scratch dir, [ATTACHMENTS] block
    prepended to agent prompt
  - ATTACHMENTS_ENABLED=false → files silently ignored
  - .docx file → .md created alongside, prompt block uses .md path (#329)
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.attachments import build_attachments_block, ingest_files

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    """Reload router.app with mocked module-level side effects and attachment root."""
    monkeypatch.setenv("SAM_BOT_TOKEN", "xoxb-sam")
    monkeypatch.setenv("SAM_APP_TOKEN", "xapp-sam")
    monkeypatch.setenv("SAM_SIGNING_SECRET", "sam-secret")

    with (
        patch("router.app.AsyncApp") as mock_app_cls,
        patch("router.app.AsyncSocketModeHandler"),
        patch("router.app.load_dotenv"),
    ):
        mock_bolt_app = MagicMock()
        mock_bolt_app.client = MagicMock()
        mock_app_cls.return_value = mock_bolt_app

        import router.app
        import router.threads.state as thread_state_mod

        importlib.reload(router.app)

        monkeypatch.setattr(router.app, "needs_curation", lambda *a, **kw: False)
        monkeypatch.setattr(router.app, "curate_agent_memory", AsyncMock())

        thread_state_mod.reset_default_store()
        monkeypatch.setattr(
            thread_state_mod,
            "DEFAULT_DB_PATH",
            str(tmp_path / "thread_state.db"),
        )

        yield router.app, tmp_path

        thread_state_mod.reset_default_store()


def _make_file(name, mimetype, size, file_id, oversize=False):
    return {
        "id": file_id,
        "name": name,
        "mimetype": mimetype,
        "size": size,
        "url_private": f"https://files.slack.com/files/{file_id}",
        "title": name,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAttachmentsIngest:
    """End-to-end ingest tests through _handle_event."""

    _AGENT = "sam"
    _CHANNEL = "C001"
    _THREAD = "1700000000.000001"

    def _base_event(self, files):
        return {
            "type": "message",
            "user": "U_HUMAN",
            "text": "here are some files",
            "channel": self._CHANNEL,
            "ts": self._THREAD,
            "thread_ts": self._THREAD,
            "files": files,
        }

    @pytest.mark.asyncio
    async def test_oversize_file_rejected(self, app_module, monkeypatch):
        """Oversize file → rejection reply posted; dispatch NOT called."""
        app, tmp_path = app_module
        monkeypatch.setenv("ATTACHMENTS_ENABLED", "1")

        oversize = _make_file("big.pdf", "application/pdf", 30 * 1024 * 1024, "F_BIG")
        event = self._base_event([oversize])

        say = AsyncMock()
        client = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with (
            patch(
                "router.app.get_agent_map",
                return_value={self._AGENT: {"container": "sam", "name": "Sam"}},
            ),
            patch(
                "router.app.config",
                {
                    "slack_credentials": {self._AGENT: {"bot_token": "xoxb-sam"}},
                    "session_timeout": 60,
                },
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": self._AGENT, "thread_history": []},
            ),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
            patch("router.app.dispatch", new_callable=AsyncMock) as mock_dispatch,
            patch("router.app._ATTACHMENTS_ROOT", str(tmp_path)),
        ):
            await app._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)

        mock_dispatch.assert_not_called()
        client.chat_postMessage.assert_called_once()
        rejection_text = client.chat_postMessage.call_args.kwargs.get("text", "")
        assert "25 MB" in rejection_text or "limit" in rejection_text.lower()

    @pytest.mark.asyncio
    async def test_valid_files_ingested_and_block_prepended(self, app_module, monkeypatch):
        """PDF + PNG within caps → files downloaded, [ATTACHMENTS] block prepended."""
        app, tmp_path = app_module
        monkeypatch.setenv("ATTACHMENTS_ENABLED", "1")

        pdf = _make_file("report.pdf", "application/pdf", 1024, "F_PDF")
        png = _make_file("screenshot.png", "image/png", 512, "F_PNG")
        event = self._base_event([pdf, png])

        say = AsyncMock()
        client = AsyncMock()

        captured_message: list[str] = []

        async def fake_dispatch(*, agent_name, message, **kw):
            captured_message.append(message)
            return {"response": "all good"}

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake content")

        with (
            patch(
                "router.app.get_agent_map",
                return_value={self._AGENT: {"container": "sam", "name": "Sam"}},
            ),
            patch(
                "router.app.config",
                {
                    "slack_credentials": {self._AGENT: {"bot_token": "xoxb-sam"}},
                    "session_timeout": 60,
                },
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": self._AGENT, "thread_history": []},
            ),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
            patch("router.app.dispatch", side_effect=fake_dispatch),
            patch(
                "router.app.parse_response",
                return_value=MagicMock(
                    has_drafts=False,
                    cleaned_text="all good",
                    draft_requests=[],
                    parse_errors=[],
                    fence_warnings=[],
                ),
            ),
            patch("router.app.detect_unbacked_claim", return_value=None),
            patch("router.app.md_to_slack", side_effect=lambda t: t),
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.app._ATTACHMENTS_ROOT", str(tmp_path)),
        ):
            await app._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)

        assert captured_message, "dispatch was not called"
        msg = captured_message[0]
        assert "[ATTACHMENTS]" in msg
        thread_dir = tmp_path / self._THREAD
        assert thread_dir.is_dir()
        stored = list(thread_dir.iterdir())
        assert len(stored) == 2

    @pytest.mark.asyncio
    async def test_flag_disabled_skips_ingest(self, app_module, monkeypatch):
        """ATTACHMENTS_ENABLED=false → files ignored; dispatch called with plain text."""
        app, tmp_path = app_module
        monkeypatch.setenv("ATTACHMENTS_ENABLED", "false")

        pdf = _make_file("report.pdf", "application/pdf", 1024, "F_PDF")
        event = self._base_event([pdf])
        event["text"] = "hello"

        say = AsyncMock()
        client = AsyncMock()
        captured: list[str] = []

        async def fake_dispatch(*, agent_name, message, **kw):
            captured.append(message)
            return {"response": "ok"}

        with (
            patch(
                "router.app.get_agent_map",
                return_value={self._AGENT: {"container": "sam", "name": "Sam"}},
            ),
            patch(
                "router.app.config",
                {
                    "slack_credentials": {self._AGENT: {"bot_token": "xoxb-sam"}},
                    "session_timeout": 60,
                },
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": self._AGENT, "thread_history": []},
            ),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
            patch("router.app.dispatch", side_effect=fake_dispatch),
            patch(
                "router.app.parse_response",
                return_value=MagicMock(
                    has_drafts=False,
                    cleaned_text="ok",
                    draft_requests=[],
                    parse_errors=[],
                    fence_warnings=[],
                ),
            ),
            patch("router.app.detect_unbacked_claim", return_value=None),
            patch("router.app.md_to_slack", side_effect=lambda t: t),
            patch("router.app._ATTACHMENTS_ROOT", str(tmp_path)),
        ):
            await app._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)

        assert captured
        msg = captured[0]
        assert "[ATTACHMENTS]" not in msg
        assert msg == "hello"
        # No files should have been written
        assert not (tmp_path / self._THREAD).exists()


# ── Office doc conversion (#329) ──────────────────────────────────────────────


class TestOfficeDocConversion:
    """Integration: .docx ingested → .md created alongside, prompt block uses .md."""

    _THREAD = "1700000001.000001"

    @pytest.mark.asyncio
    async def test_docx_converted_and_md_in_block(self, tmp_path):
        """Ingest a .docx: .md is created alongside it; prompt block references .md not .docx."""
        docx_file = {
            "id": "F_DOCX",
            "name": "report.docx",
            "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 1024,
            "url_private": "https://files.slack.com/files/F_DOCX",
        }

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake docx content")

        def fake_convert(src_path, dest_path):
            dest_path.write_text("# Report\n\nSome content here.", encoding="utf-8")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._markitdown_convert", side_effect=fake_convert),
        ):
            paths, warnings = await ingest_files(
                [docx_file],
                self._THREAD,
                "xoxb-token",
                attachments_root=str(tmp_path),
            )

        thread_dir = tmp_path / self._THREAD

        # Original .docx retained in scratch dir
        assert (thread_dir / "report.docx").exists()
        # Converted .md created alongside
        assert (thread_dir / "report.md").exists()
        assert (thread_dir / "report.md").read_text(encoding="utf-8") == "# Report\n\nSome content here."

        # Prompt block lists .md, not .docx
        assert len(paths) == 1
        assert paths[0].endswith("report.md")
        block = build_attachments_block(paths)
        assert "report.md" in block
        assert "report.docx" not in block
        assert warnings == []
