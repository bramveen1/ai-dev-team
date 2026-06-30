"""Unit tests for router.attachments (#328, #329).

Covers: filename sanitisation, mimetype allowlist, cap enforcement,
external-link skip, prompt block building, feature flag, and Office
doc conversion via markitdown.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from router.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_MSG,
    MAX_THREAD_BYTES,
    OFFICE_EXTENSIONS,
    _download_url,
    attachments_enabled,
    build_attachments_block,
    convert_office_to_markdown,
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
        paths, warnings = await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))
        assert paths == []
        assert warnings == []

    @pytest.mark.asyncio
    async def test_successful_download(self, tmp_path):
        files = [{"id": "F1", "name": "report.pdf", "mimetype": "application/pdf", "url_private": "https://x"}]
        with patch("router.attachments._download_url", new_callable=AsyncMock) as mock_dl:
            mock_dl.return_value = None
            paths, warnings = await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))

        assert len(paths) == 1
        assert "report.pdf" in paths[0]
        assert paths[0].startswith("/")
        assert warnings == []

    @pytest.mark.asyncio
    async def test_failed_download_logged_not_raised(self, tmp_path):
        files = [{"id": "F1", "name": "report.pdf", "url_private": "https://x"}]
        with patch("router.attachments._download_url", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            paths, warnings = await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))
        assert paths == []
        assert warnings == []

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
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        # The new file should have been stored with a prefixed name
        dest_name = Path(paths[0]).name
        assert dest_name != "report.pdf"
        assert "F999" in dest_name

    @pytest.mark.asyncio
    async def test_office_file_converted_to_md(self, tmp_path):
        """Office file: .docx download triggers conversion; .md path returned."""
        files = [
            {
                "id": "F_DOC",
                "name": "report.docx",
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "url_private": "https://x",
            }
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake docx")

        def fake_convert(src, dest):
            dest.write_text("# Report", encoding="utf-8")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._markitdown_convert", side_effect=fake_convert),
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        assert len(paths) == 1
        assert paths[0].endswith("report.md")
        assert warnings == []

    @pytest.mark.asyncio
    async def test_mtime_pre_bumped_before_downloads_issue401(self, tmp_path):
        """ingest_files must call os.utime immediately after mkdir (TOCTOU fix, #401).

        The mtime of the thread directory must be refreshed *before* any
        download begins so that the janitor cannot see a stale mtime during
        the download window.
        """
        files = [{"id": "F1", "name": "report.pdf", "url_private": "https://x"}]
        utime_calls: list[tuple] = []
        download_calls: list[int] = []

        original_utime = os.utime

        def recording_utime(path, times):
            utime_calls.append((str(path), times))
            original_utime(path, times)

        async def recording_download(url, token, dest):
            download_calls.append(len(utime_calls))  # how many utimes before this download

        with (
            patch("router.attachments.os.utime", side_effect=recording_utime),
            patch("router.attachments._download_url", side_effect=recording_download),
        ):
            await ingest_files(files, "T1", "xoxb-token", attachments_root=str(tmp_path))

        thread_dir_str = str(tmp_path / "T1")
        pre_download_utime = [c for c in utime_calls if c[0] == thread_dir_str]
        assert pre_download_utime, "os.utime must be called on the thread dir"
        # The pre-bump utime must happen before any download
        assert download_calls and download_calls[0] >= 1, "os.utime must be called before the first download begins"

    @pytest.mark.asyncio
    async def test_same_stem_office_files_produce_distinct_markdown(self, tmp_path):
        """Regression for #486: q3.docx and q3.xlsx must not both convert to q3.md."""
        files = [
            {
                "id": "F_DOC",
                "name": "q3.docx",
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "url_private": "https://x/q3.docx",
            },
            {
                "id": "F_XLS",
                "name": "q3.xlsx",
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "url_private": "https://x/q3.xlsx",
            },
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake " + dest.name.encode())

        def fake_convert(src, dest):
            dest.write_text(f"# Content from {src.name}", encoding="utf-8")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._markitdown_convert", side_effect=fake_convert),
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        assert len(paths) == 2, f"Expected 2 paths, got {paths}"
        assert warnings == []
        assert paths[0] != paths[1], "Both Office files must produce distinct .md paths"
        assert all(p.endswith(".md") for p in paths)
        content0 = Path(paths[0]).read_text(encoding="utf-8")
        content1 = Path(paths[1]).read_text(encoding="utf-8")
        assert content0 != content1, "Second file silently overwrote first (data loss regression)"

    @pytest.mark.asyncio
    async def test_office_conversion_does_not_clobber_same_stem_nonoffice(self, tmp_path):
        """Regression for #501: report.docx must not overwrite the user's report.md.

        The critical ordering is docx-first: without pre-reservation of all
        download targets, the docx conversion sees an empty existing_names set
        and writes to report.md before the user's report.md is downloaded.
        After the fix the converted docx must land at a non-colliding name
        (e.g. dup_report.md) and the user's report.md must keep its name.
        """
        USER_MD_CONTENT = b"# User original markdown"
        CONVERTED_CONTENT = "# Converted from docx"

        files = [
            {
                "id": "F_DOC",
                "name": "report.docx",
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "url_private": "https://x/report.docx",
            },
            {
                "id": "F_MD",
                "name": "report.md",
                "mimetype": "text/markdown",
                "url_private": "https://x/report.md",
            },
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake docx" if url.endswith(".docx") else USER_MD_CONTENT)

        def fake_convert(src, dest):
            dest.write_text(CONVERTED_CONTENT, encoding="utf-8")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._markitdown_convert", side_effect=fake_convert),
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        assert len(paths) == 2, f"Expected 2 paths, got {paths}"
        assert warnings == []

        # The user's report.md must be preserved at the report.md filename.
        md_path = next((p for p in paths if Path(p).name == "report.md"), None)
        assert md_path is not None, "User's report.md must appear in paths as report.md"
        assert Path(md_path).read_bytes() == USER_MD_CONTENT, "report.md must contain the user's original content"

        # The converted docx must land at a non-colliding name.
        other_paths = [p for p in paths if Path(p).name != "report.md"]
        assert len(other_paths) == 1, "Converted docx must appear under a non-colliding name"
        assert Path(other_paths[0]).read_text(encoding="utf-8") == CONVERTED_CONTENT

    @pytest.mark.asyncio
    async def test_office_conversion_failure_yields_warning(self, tmp_path):
        """When conversion fails, no path is added and a warning message is returned."""
        files = [
            {
                "id": "F_DOC",
                "name": "report.docx",
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "url_private": "https://x",
            }
        ]

        async def fake_download(url, token, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake docx")

        with (
            patch("router.attachments._download_url", side_effect=fake_download),
            patch("router.attachments._markitdown_convert", side_effect=RuntimeError("parse error")),
        ):
            paths, warnings = await ingest_files(files, "T1", "token", attachments_root=str(tmp_path))

        assert paths == []
        assert len(warnings) == 1
        assert "report.docx" in warnings[0]


# ── convert_office_to_markdown ────────────────────────────────────────────────


class TestConvertOfficeToMarkdown:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ext", [".docx", ".xlsx", ".pptx"])
    async def test_success(self, tmp_path, ext):
        """Each supported Office extension is converted; .md written alongside."""
        src = tmp_path / f"file{ext}"
        src.write_bytes(b"fake office content")
        expected_md = src.with_suffix(".md")

        def fake_convert(src_path, dest_path):
            dest_path.write_text("# Heading\n\nContent", encoding="utf-8")

        with patch("router.attachments._markitdown_convert", side_effect=fake_convert):
            result = await convert_office_to_markdown(src)

        assert result == expected_md
        assert expected_md.read_text(encoding="utf-8") == "# Heading\n\nContent"

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, tmp_path):
        """Timeout during conversion returns None and logs a warning."""
        src = tmp_path / "doc.docx"
        src.write_bytes(b"fake")

        async def _raise_timeout(coro, timeout):
            coro.close()  # avoid "coroutine never awaited" warning
            raise asyncio.TimeoutError()

        with patch.object(asyncio, "wait_for", new=_raise_timeout):
            result = await convert_office_to_markdown(src)

        assert result is None

    @pytest.mark.asyncio
    async def test_conversion_exception_returns_none(self, tmp_path):
        """Exception raised by markitdown returns None and logs a warning."""
        src = tmp_path / "doc.docx"
        src.write_bytes(b"fake")

        with patch("router.attachments._markitdown_convert", side_effect=RuntimeError("parse error")):
            result = await convert_office_to_markdown(src)

        assert result is None

    @pytest.mark.asyncio
    async def test_unsupported_extension_returns_none(self, tmp_path):
        """Non-Office extension returns None without calling markitdown."""
        src = tmp_path / "file.txt"
        src.write_bytes(b"text content")

        with patch("router.attachments._markitdown_convert") as mock_convert:
            result = await convert_office_to_markdown(src)

        assert result is None
        mock_convert.assert_not_called()

    def test_office_extensions_constant(self):
        assert ".docx" in OFFICE_EXTENSIONS
        assert ".xlsx" in OFFICE_EXTENSIONS
        assert ".pptx" in OFFICE_EXTENSIONS
        assert ".pdf" not in OFFICE_EXTENSIONS
        assert ".txt" not in OFFICE_EXTENSIONS


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


# ── _download_url ─────────────────────────────────────────────────────────────


def _make_aiohttp_mock(chunks: list[bytes], status: int = 200, content_length: str | None = None):
    """Build a nested aiohttp mock: ClientSession → GET → response."""
    from unittest.mock import MagicMock

    async def _iter_chunked(_chunk_size):
        for chunk in chunks:
            yield chunk

    resp = MagicMock()
    resp.status = status
    resp.headers = {}
    if content_length is not None:
        resp.headers["Content-Length"] = content_length
    resp.content.iter_chunked = _iter_chunked

    resp_ctx = MagicMock()
    resp_ctx.__aenter__ = AsyncMock(return_value=resp)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)

    get_ctx = MagicMock()
    get_ctx.__aenter__ = AsyncMock(return_value=resp_ctx)

    session = MagicMock()
    session.get = MagicMock(return_value=resp_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    return session_ctx


class TestDownloadUrl:
    @pytest.mark.asyncio
    async def test_success_under_cap_writes_dest(self, tmp_path):
        """A body well under MAX_FILE_BYTES is written to dest; no .part file left."""
        dest = tmp_path / "file.pdf"
        data = b"hello world"
        chunks = [data]

        async def _iter_chunked(_size):
            for c in chunks:
                yield c

        resp = AsyncMock()
        resp.status = 200
        resp.headers = {}
        resp.content.iter_chunked = _iter_chunked

        with patch("aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=MockSession.return_value)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value.get.return_value.__aenter__ = AsyncMock(return_value=resp)
            MockSession.return_value.get.return_value.__aexit__ = AsyncMock(return_value=False)

            await _download_url("https://example.com/file.pdf", "xoxb-token", dest)

        assert dest.exists()
        assert dest.read_bytes() == data
        # .part file must be gone after success
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    @pytest.mark.asyncio
    async def test_oversized_body_rejected_no_dest(self, tmp_path):
        """Body larger than MAX_FILE_BYTES with no/small declared size is rejected.

        Regression for #421: validate_files only checks Slack's self-reported
        ``size`` field; _download_url must enforce the cap on actual bytes.
        """
        dest = tmp_path / "big.pdf"
        # Single chunk just over the cap
        big_chunk = b"x" * (MAX_FILE_BYTES + 1)

        async def _iter_chunked(_size):
            yield big_chunk

        resp = AsyncMock()
        resp.status = 200
        resp.headers = {}
        resp.content.iter_chunked = _iter_chunked

        with patch("aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=MockSession.return_value)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value.get.return_value.__aenter__ = AsyncMock(return_value=resp)
            MockSession.return_value.get.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="MAX_FILE_BYTES"):
                await _download_url("https://example.com/big.pdf", "xoxb-token", dest)

        # dest must NOT exist — no partial file left behind
        assert not dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    @pytest.mark.asyncio
    async def test_content_length_over_cap_rejected_early(self, tmp_path):
        """Content-Length exceeding MAX_FILE_BYTES causes early rejection before reading body."""
        dest = tmp_path / "file.pdf"
        oversized_cl = str(MAX_FILE_BYTES + 1)

        body_read = []

        async def _iter_chunked(_size):
            body_read.append(True)
            yield b"data"

        resp = AsyncMock()
        resp.status = 200
        resp.headers = {"Content-Length": oversized_cl}
        resp.content.iter_chunked = _iter_chunked

        with patch("aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=MockSession.return_value)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value.get.return_value.__aenter__ = AsyncMock(return_value=resp)
            MockSession.return_value.get.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="Content-Length"):
                await _download_url("https://example.com/file.pdf", "xoxb-token", dest)

        # Body was never streamed
        assert not body_read
        assert not dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    @pytest.mark.asyncio
    async def test_mid_stream_failure_leaves_no_file(self, tmp_path):
        """An exception mid-stream removes the .part file and leaves dest absent."""
        dest = tmp_path / "file.pdf"

        async def _iter_chunked(_size):
            yield b"partial data"
            raise RuntimeError("network dropped")

        resp = AsyncMock()
        resp.status = 200
        resp.headers = {}
        resp.content.iter_chunked = _iter_chunked

        with patch("aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=MockSession.return_value)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value.get.return_value.__aenter__ = AsyncMock(return_value=resp)
            MockSession.return_value.get.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="network dropped"):
                await _download_url("https://example.com/file.pdf", "xoxb-token", dest)

        assert not dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    @pytest.mark.asyncio
    async def test_http_error_leaves_no_file(self, tmp_path):
        """HTTP 403 raises RuntimeError and leaves no file."""
        dest = tmp_path / "file.pdf"

        resp = AsyncMock()
        resp.status = 403
        resp.headers = {}

        with patch("aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(return_value=MockSession.return_value)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value.get.return_value.__aenter__ = AsyncMock(return_value=resp)
            MockSession.return_value.get.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="HTTP 403"):
                await _download_url("https://example.com/file.pdf", "xoxb-token", dest)

        assert not dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()
