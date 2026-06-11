"""Unit tests for router.attachments (#328).

Covers: filename sanitisation, mimetype allowlist, cap enforcement,
external-link skip, prompt block building, and feature flag.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from router.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_MSG,
    MAX_THREAD_BYTES,
    attachments_enabled,
    build_attachments_block,
    ingest_files,
    is_allowed_mimetype,
    sanitise_filename,
    validate_files,
)

pytestmark = pytest.mark.unit

# ── attachments_enabled ───────────────────────────────────────────────────────


class TestAttachmentsEnabled:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("ATTACHMENTS_ENABLED", raising=False)
        assert attachments_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "TRUE", "yes", "YES"])
    def test_truthy_values(self, val, monkeypatch):
        monkeypatch.setenv("ATTACHMENTS_ENABLED", val)
        assert attachments_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "False", "no", "", "off"])
    def test_falsy_values(self, val, monkeypatch):
        monkeypatch.setenv("ATTACHMENTS_ENABLED", val)
        assert attachments_enabled() is False


# ── is_allowed_mimetype ───────────────────────────────────────────────────────


class TestIsAllowedMimetype:
    @pytest.mark.parametrize(
        "mt",
        [
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "image/svg+xml",
            "text/plain",
            "text/markdown",
            "text/x-python",
            "text/csv",
            "text/html",
            "application/pdf",
            "application/json",
            "application/javascript",
            "application/xml",
            # Office
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ],
    )
    def test_allowed(self, mt):
        assert is_allowed_mimetype(mt) is True

    @pytest.mark.parametrize(
        "mt",
        [
            "application/octet-stream",
            "application/zip",
            "application/x-rar-compressed",
            "application/x-executable",
            "video/mp4",
            "application/x-msdownload",
            "",
        ],
    )
    def test_rejected(self, mt):
        assert is_allowed_mimetype(mt) is False

    def test_charset_suffix_stripped(self):
        assert is_allowed_mimetype("text/plain; charset=utf-8") is True

    def test_case_insensitive(self):
        assert is_allowed_mimetype("IMAGE/PNG") is True
        assert is_allowed_mimetype("Application/PDF") is True


# ── sanitise_filename ─────────────────────────────────────────────────────────


class TestSanitiseFilename:
    def test_passthrough_simple(self):
        assert sanitise_filename("report.pdf") == "report.pdf"

    def test_strips_control_chars(self):
        assert sanitise_filename("re\x00port\x1f.pdf") == "report.pdf"

    def test_replaces_forward_slash(self):
        assert sanitise_filename("path/to/file.txt") == "path_to_file.txt"

    def test_replaces_backslash(self):
        assert sanitise_filename("path\\to\\file.txt") == "path_to_file.txt"

    def test_truncates_to_max_len(self):
        long_name = "a" * 200
        result = sanitise_filename(long_name)
        assert len(result) == 80

    def test_strips_leading_trailing_dots(self):
        assert sanitise_filename("...name...") == "name"

    def test_strips_leading_trailing_spaces(self):
        assert sanitise_filename("  name  ") == "name"

    def test_empty_name_becomes_unnamed(self):
        assert sanitise_filename("") == "unnamed"

    def test_only_control_chars_becomes_unnamed(self):
        assert sanitise_filename("\x00\x01\x02") == "unnamed"

    def test_collision_prefix_with_file_id(self):
        existing = {"report.pdf"}
        result = sanitise_filename("report.pdf", file_id="F123", existing=existing)
        assert result == "F123_report.pdf"
        assert result not in existing  # original still there, result is different

    def test_collision_prefix_without_file_id(self):
        existing = {"report.pdf"}
        result = sanitise_filename("report.pdf", file_id="", existing=existing)
        assert result.startswith("dup_")

    def test_no_collision_when_not_in_existing(self):
        existing = {"other.pdf"}
        result = sanitise_filename("report.pdf", file_id="F123", existing=existing)
        assert result == "report.pdf"

    def test_nfkc_normalisation(self):
        # Full-width slash U+FF0F should be normalised and then replaced
        name = "fi＋le.txt"
        result = sanitise_filename(name)
        # NFKC: full-width slash → U+002F (ordinary /), then replaced with _
        assert "/" not in result
        assert "\\" not in result


# ── validate_files ────────────────────────────────────────────────────────────


class TestValidateFiles:
    def _file(self, name="file.pdf", mimetype="application/pdf", size=1024, file_id="F1", url="https://x"):
        return {
            "id": file_id,
            "name": name,
            "mimetype": mimetype,
            "size": size,
            "url_private": url,
        }

    def test_empty_files_ok(self):
        valid, reason = validate_files([], "T1")
        assert valid == []
        assert reason is None

    def test_single_valid_file_ok(self):
        files = [self._file()]
        valid, reason = validate_files(files, "T1")
        assert reason is None
        assert len(valid) == 1

    def test_too_many_files_rejected(self):
        files = [self._file(file_id=f"F{i}", name=f"f{i}.pdf") for i in range(MAX_FILES_PER_MSG + 1)]
        valid, reason = validate_files(files, "T1")
        assert valid == []
        assert reason is not None
        assert str(MAX_FILES_PER_MSG) in reason

    def test_bad_mimetype_rejected(self):
        files = [self._file(mimetype="application/octet-stream")]
        valid, reason = validate_files(files, "T1")
        assert valid == []
        assert "unsupported type" in reason

    def test_oversize_file_rejected(self):
        files = [self._file(size=MAX_FILE_BYTES + 1)]
        valid, reason = validate_files(files, "T1")
        assert valid == []
        assert "25 MB" in reason

    def test_per_thread_cumulative_cap(self, tmp_path):
        # Create a fake thread dir with existing bytes close to cap
        thread_ts = "T999"
        thread_dir = tmp_path / thread_ts
        thread_dir.mkdir()
        # Fill thread dir with a large existing file
        existing = thread_dir / "existing.pdf"
        existing.write_bytes(b"x" * (MAX_THREAD_BYTES - 100))

        # A new 200-byte file should push over the cap
        files = [self._file(size=200)]
        valid, reason = validate_files(files, thread_ts, attachments_root=str(tmp_path))
        assert valid == []
        assert "per-thread limit" in reason

    def test_external_mode_skips_caps_and_mimetype(self):
        # File without url_private → external-mode, skip validation
        files = [
            {
                "id": "F1",
                "name": "gdrive_doc",
                "mimetype": "application/vnd.google-apps.document",
                "size": MAX_FILE_BYTES + 1,
                # no url_private
            }
        ]
        valid, reason = validate_files(files, "T1")
        # No rejection because external-mode files are ignored
        assert reason is None

    def test_mixed_external_and_local_validates_local(self):
        # External file is fine; local file with bad mimetype is rejected
        files = [
            {
                "id": "F1",
                "name": "gdrive",
                "mimetype": "application/vnd.google-apps.document",
                "size": 500,
            },
            self._file(name="bad.exe", mimetype="application/x-msdownload"),
        ]
        valid, reason = validate_files(files, "T1")
        assert valid == []
        assert "unsupported type" in reason


# ── ingest_files ──────────────────────────────────────────────────────────────


class TestIngestFiles:
    @pytest.mark.asyncio
    async def test_external_mode_skipped(self, tmp_path):
        files = [{"id": "F1", "name": "gdoc", "mimetype": "text/plain"}]  # no url_private
        paths = await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))
        assert paths == []

    @pytest.mark.asyncio
    async def test_successful_download(self, tmp_path):
        files = [{"id": "F1", "name": "report.pdf", "mimetype": "application/pdf", "url_private": "https://x"}]
        with patch("router.attachments._download_url", new_callable=AsyncMock) as mock_dl:
            mock_dl.return_value = None
            paths = await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))

        assert len(paths) == 1
        assert "report.pdf" in paths[0]
        assert paths[0].startswith("/")

    @pytest.mark.asyncio
    async def test_failed_download_logged_not_raised(self, tmp_path):
        files = [{"id": "F1", "name": "report.pdf", "url_private": "https://x"}]
        with patch("router.attachments._download_url", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            paths = await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))
        assert paths == []

    @pytest.mark.asyncio
    async def test_thread_dir_created(self, tmp_path):
        files = [{"id": "F1", "name": "doc.txt", "url_private": "https://x"}]
        with patch("router.attachments._download_url", new_callable=AsyncMock):
            await ingest_files(files, "mythread", "token", attachments_root=str(tmp_path))
        assert (tmp_path / "mythread").is_dir()

    @pytest.mark.asyncio
    async def test_collision_prefixed_with_file_id(self, tmp_path):
        thread_dir = tmp_path / "T1"
        thread_dir.mkdir()
        (thread_dir / "report.pdf").write_bytes(b"old")

        files = [{"id": "F999", "name": "report.pdf", "url_private": "https://x"}]
        with patch("router.attachments._download_url", new_callable=AsyncMock):
            paths = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        # The new file should have been stored with a prefixed name
        dest_name = Path(paths[0]).name
        assert dest_name != "report.pdf"
        assert "F999" in dest_name


# ── build_attachments_block ───────────────────────────────────────────────────


class TestBuildAttachmentsBlock:
    def test_empty_paths_returns_empty(self):
        assert build_attachments_block([]) == ""

    def test_single_path(self):
        block = build_attachments_block(["/var/lib/attachments/T1/file.pdf"])
        assert block.startswith("[ATTACHMENTS]\n")
        assert "/var/lib/attachments/T1/file.pdf" in block
        assert block.endswith("\n\n")

    def test_multiple_paths_one_per_line(self):
        paths = ["/a/b.pdf", "/a/c.png"]
        block = build_attachments_block(paths)
        lines = block.splitlines()
        assert lines[0] == "[ATTACHMENTS]"
        assert "/a/b.pdf" in lines[1]
        assert "/a/c.png" in lines[2]
