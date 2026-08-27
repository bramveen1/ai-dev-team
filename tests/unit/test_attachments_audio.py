"""Unit tests for audio attachment ingest via OpenAI Whisper STT (#804).

Covers: the AUDIO_INGEST_ENABLED feature flag, the mimetype-allowlist gate,
the transcribe_audio_to_text() converter, and the ingest_files() wiring —
including the invariant that one bad/rejected audio clip must never drop
other attachments in the same message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from router.attachments import (
    audio_ingest_enabled,
    ingest_files,
    is_allowed_mimetype,
    transcribe_audio_to_text,
    validate_files,
)

pytestmark = pytest.mark.unit


# ── audio_ingest_enabled ───────────────────────────────────────────────────────


class TestAudioIngestEnabled:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("AUDIO_INGEST_ENABLED", raising=False)
        assert audio_ingest_enabled() is False

    def test_empty_env_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", "")
        assert audio_ingest_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "yes"])
    def test_truthy_values(self, val, monkeypatch):
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", val)
        assert audio_ingest_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_falsy_values(self, val, monkeypatch):
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", val)
        assert audio_ingest_enabled() is False


# ── is_allowed_mimetype (audio gate) ───────────────────────────────────────────


class TestAudioMimetypeGate:
    def test_audio_rejected_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("AUDIO_INGEST_ENABLED", raising=False)
        assert is_allowed_mimetype("audio/mp4") is False
        assert is_allowed_mimetype("audio/mpeg") is False

    def test_audio_allowed_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", "1")
        assert is_allowed_mimetype("audio/mp4") is True
        assert is_allowed_mimetype("AUDIO/MP4") is True


# ── validate_files: batch-level gate ───────────────────────────────────────────


class TestValidateFilesAudioGate:
    def _audio_file(self, name="voice.m4a", mimetype="audio/mp4", size=1024):
        return {
            "id": "F1",
            "name": name,
            "mimetype": mimetype,
            "size": size,
            "url_private": "https://x",
        }

    def test_audio_rejected_when_flag_off(self, monkeypatch):
        """audio/mp4 rejected with the existing message when AUDIO_INGEST_ENABLED is false."""
        monkeypatch.delenv("AUDIO_INGEST_ENABLED", raising=False)
        valid, reason = validate_files([self._audio_file()], "T1")
        assert valid == []
        assert reason is not None
        assert "unsupported type" in reason
        assert "audio/mp4" in reason

    def test_audio_accepted_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", "1")
        valid, reason = validate_files([self._audio_file()], "T1")
        assert reason is None
        assert len(valid) == 1


# ── transcribe_audio_to_text ───────────────────────────────────────────────────


class TestTranscribeAudioToText:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")
        src = tmp_path / "voice.m4a"
        src.write_bytes(b"fake audio content")

        with patch("router.attachments._whisper_transcribe", new=AsyncMock(return_value="hello world")):
            result = await transcribe_audio_to_text(src)

        assert result == src.with_suffix(".txt")
        assert result.read_text(encoding="utf-8") == "hello world"

    @pytest.mark.asyncio
    async def test_no_key_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_WHISPER_KEY", raising=False)
        src = tmp_path / "voice.m4a"
        src.write_bytes(b"fake audio content")

        with patch("router.attachments._whisper_transcribe", new=AsyncMock()) as mock_call:
            result = await transcribe_audio_to_text(src)

        assert result is None
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")
        src = tmp_path / "voice.m4a"
        src.write_bytes(b"fake audio content")

        async def _raise_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        with (
            patch("router.attachments._whisper_transcribe", new=AsyncMock(return_value="text")),
            patch.object(asyncio, "wait_for", new=_raise_timeout),
        ):
            result = await transcribe_audio_to_text(src)

        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")
        src = tmp_path / "voice.m4a"
        src.write_bytes(b"fake audio content")

        with patch("router.attachments._whisper_transcribe", new=AsyncMock(side_effect=RuntimeError("503"))):
            result = await transcribe_audio_to_text(src)

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_transcript_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")
        src = tmp_path / "voice.m4a"
        src.write_bytes(b"fake audio content")

        with patch("router.attachments._whisper_transcribe", new=AsyncMock(return_value="   ")):
            result = await transcribe_audio_to_text(src)

        assert result is None
        assert not src.with_suffix(".txt").exists()

    @pytest.mark.asyncio
    async def test_collision_free_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")
        src = tmp_path / "voice.m4a"
        src.write_bytes(b"fake audio content")
        existing = {"voice.txt"}

        with patch("router.attachments._whisper_transcribe", new=AsyncMock(return_value="hi")):
            result = await transcribe_audio_to_text(src, existing_names=existing)

        assert result.name != "voice.txt"
        assert result.name in existing


# ── ingest_files wiring ────────────────────────────────────────────────────────


class TestIngestFilesAudio:
    @pytest.mark.asyncio
    async def test_audio_transcribed_to_sidecar_when_flag_on(self, tmp_path, monkeypatch):
        """With flag on + stubbed Whisper client, a fixture clip yields a .txt
        sidecar injected as context, original retained."""
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", "1")
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")

        files = [
            {
                "id": "F_AUDIO",
                "name": "voice.m4a",
                "mimetype": "audio/mp4",
                "url_private": "https://x/voice.m4a",
            }
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake audio bytes")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._whisper_transcribe", new=AsyncMock(return_value="voice note text")),
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        assert warnings == []
        assert len(paths) == 1
        assert paths[0].endswith("voice.txt")
        assert "voice note text" in Path(paths[0]).read_text(encoding="utf-8")
        # Original audio file retained on disk even though it isn't in the
        # [ATTACHMENTS] paths list (same contract as the Office .md sidecar).
        assert (tmp_path / "T1" / "voice.m4a").exists()

    @pytest.mark.asyncio
    async def test_transcription_failure_does_not_drop_other_attachments(self, tmp_path, monkeypatch):
        """Audio failure + a co-attached image: image still ingests, audio
        produces a warning."""
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", "1")
        monkeypatch.setenv("OPENAI_WHISPER_KEY", "sk-test")

        files = [
            {
                "id": "F_AUDIO",
                "name": "voice.m4a",
                "mimetype": "audio/mp4",
                "url_private": "https://x/voice.m4a",
            },
            {
                "id": "F_IMG",
                "name": "photo.png",
                "mimetype": "image/png",
                "url_private": "https://x/photo.png",
            },
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake bytes")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._whisper_transcribe", new=AsyncMock(side_effect=RuntimeError("upstream 500"))),
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        assert len(warnings) == 1
        assert "voice.m4a" in warnings[0]
        assert len(paths) == 1
        assert paths[0].endswith("photo.png")

    @pytest.mark.asyncio
    async def test_flag_on_no_key_fails_loud(self, tmp_path, monkeypatch):
        """Flag on, no key: per-attachment warning, batch not failed."""
        monkeypatch.setenv("AUDIO_INGEST_ENABLED", "1")
        monkeypatch.delenv("OPENAI_WHISPER_KEY", raising=False)

        files = [
            {
                "id": "F_AUDIO",
                "name": "voice.m4a",
                "mimetype": "audio/mp4",
                "url_private": "https://x/voice.m4a",
            },
            {
                "id": "F_IMG",
                "name": "photo.png",
                "mimetype": "image/png",
                "url_private": "https://x/photo.png",
            },
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake bytes")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._whisper_transcribe", new=AsyncMock()) as mock_call,
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        mock_call.assert_not_called()
        assert len(warnings) == 1
        assert "no key" in warnings[0]
        assert len(paths) == 1
        assert paths[0].endswith("photo.png")
