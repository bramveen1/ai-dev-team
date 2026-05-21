"""Unit tests for router.log_buffer — in-memory ring buffer with redaction.

Covers:

- ``redact`` scrubs PAT-bearing URLs, GitHub tokens, bearer headers,
  and user_id params without mangling innocent text.
- ``LogBuffer.get_recent`` honours ``tail`` and ``max_bytes`` caps.
- ``LogBuffer.get_recent`` returns lines in chronological order.
- ``install`` / ``get_buffer`` module-level helpers.
- ``/logs`` HTTP endpoint returns JSON from the buffer (via healthz tests).
"""

from __future__ import annotations

import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from router import healthz, log_buffer
from router.log_buffer import LogBuffer, redact

pytestmark = pytest.mark.unit


# ── Redaction ──────────────────────────────────────────────────────────────


class TestRedact:
    def test_scrubs_pat_url(self) -> None:
        line = "cloning https://x-access-token:ghp_ABCDEF1234567890@github.com/org/repo"
        out = redact(line)
        assert "ghp_" not in out
        assert "[REDACTED]" in out

    def test_scrubs_github_token(self) -> None:
        out = redact("token value ghp_ABC123defGHI456jkl789")
        assert "ghp_" not in out
        assert "[REDACTED" in out

    def test_scrubs_bearer_header(self) -> None:
        out = redact("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJSUzI1NiJ9" not in out
        assert "[REDACTED]" in out

    def test_scrubs_token_header(self) -> None:
        out = redact("Authorization: token ghp_mysecrettoken123456")
        assert "ghp_mysecrettoken123456" not in out

    def test_scrubs_user_id_param(self) -> None:
        out = redact("GET /api?user_id=U0123456789&other=keep")
        assert "U0123456789" not in out
        assert "other=keep" in out

    def test_innocent_text_unchanged(self) -> None:
        line = "2026-05-20 INFO router: dispatching issue #218"
        assert redact(line) == line

    def test_empty_string(self) -> None:
        assert redact("") == ""


# ── LogBuffer ring buffer ──────────────────────────────────────────────────


class TestLogBuffer:
    def _make_buffer(self, maxlines: int = 100) -> LogBuffer:
        buf = LogBuffer(maxlines=maxlines)
        buf.setFormatter(logging.Formatter("%(message)s"))
        return buf

    def _emit(self, buf: LogBuffer, message: str, level: int = logging.INFO) -> None:
        record = logging.LogRecord(
            name="test",
            level=level,
            pathname="",
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )
        buf.emit(record)

    def test_stores_lines(self) -> None:
        buf = self._make_buffer()
        self._emit(buf, "hello")
        self._emit(buf, "world")
        lines = buf.get_recent()
        assert "hello" in lines
        assert "world" in lines

    def test_tail_cap_limits_output(self) -> None:
        buf = self._make_buffer()
        for i in range(50):
            self._emit(buf, f"line {i}")
        lines = buf.get_recent(tail=10)
        assert len(lines) == 10

    def test_chronological_order(self) -> None:
        buf = self._make_buffer()
        for i in range(5):
            self._emit(buf, f"line {i}")
        lines = buf.get_recent(tail=5)
        assert lines == [f"line {i}" for i in range(5)]

    def test_ring_buffer_drops_oldest(self) -> None:
        buf = self._make_buffer(maxlines=3)
        for i in range(5):
            self._emit(buf, f"line {i}")
        lines = buf.get_recent(tail=10)
        assert len(lines) == 3
        assert "line 2" in lines[0]
        assert "line 4" in lines[2]

    def test_max_bytes_trims_from_front(self) -> None:
        buf = self._make_buffer()
        for i in range(100):
            self._emit(buf, "x" * 100)  # each line ~100 bytes
        lines = buf.get_recent(tail=100, max_bytes=500)
        total_bytes = sum(len(ln) + 1 for ln in lines)
        assert total_bytes <= 500

    def test_tail_hard_cap_enforced(self) -> None:
        buf = self._make_buffer(maxlines=1000)
        for i in range(600):
            self._emit(buf, f"line {i}")
        lines = buf.get_recent(tail=600)  # ask for more than the hard max
        assert len(lines) <= log_buffer.MAX_TAIL_LINES

    def test_max_bytes_hard_cap_enforced(self) -> None:
        buf = self._make_buffer(maxlines=1000)
        for i in range(100):
            self._emit(buf, "x" * 1000)
        lines = buf.get_recent(tail=100, max_bytes=300_000)
        total = sum(len(ln) + 1 for ln in lines)
        assert total <= log_buffer.MAX_BYTES

    def test_empty_buffer_returns_empty_list(self) -> None:
        buf = self._make_buffer()
        assert buf.get_recent() == []

    def test_redaction_applied_on_emit(self) -> None:
        buf = self._make_buffer()
        self._emit(buf, "token ghp_SECRETTOKEN12345678")
        lines = buf.get_recent()
        assert "ghp_SECRETTOKEN" not in "\n".join(lines)


# ── install / get_buffer ───────────────────────────────────────────────────


class TestInstall:
    def setup_method(self) -> None:
        # Remove any previously installed buffer between tests.
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h, LogBuffer)]
        import router.log_buffer as lb

        lb._buffer = None

    def test_install_returns_buffer(self) -> None:
        buf = log_buffer.install()
        assert isinstance(buf, LogBuffer)

    def test_get_buffer_returns_installed(self) -> None:
        buf = log_buffer.install()
        assert log_buffer.get_buffer() is buf

    def test_get_buffer_none_before_install(self) -> None:
        assert log_buffer.get_buffer() is None

    def test_install_is_idempotent(self) -> None:
        log_buffer.install()
        buf2 = log_buffer.install()
        root = logging.getLogger()
        buffer_handlers = [h for h in root.handlers if isinstance(h, LogBuffer)]
        assert len(buffer_handlers) == 1
        assert buffer_handlers[0] is buf2


# ── /logs HTTP endpoint ────────────────────────────────────────────────────


class TestLogsEndpoint:
    @pytest.fixture(autouse=True)
    def _install_fresh_buffer(self):
        buf = log_buffer.install()
        yield buf
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h, LogBuffer)]
        import router.log_buffer as lb

        lb._buffer = None

    @pytest.mark.asyncio
    async def test_returns_200_with_lines(self, _install_fresh_buffer) -> None:
        buf = _install_fresh_buffer
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello router", (), None)
        record.getMessage()
        buf.emit(record)

        async with TestClient(TestServer(healthz.build_app())) as client:
            resp = await client.get("/logs")
            assert resp.status == 200
            body = await resp.json()
        assert body["status"] == "ok"
        assert any("hello router" in ln for ln in body["lines"])

    @pytest.mark.asyncio
    async def test_tail_param_limits_lines(self, _install_fresh_buffer) -> None:
        buf = _install_fresh_buffer
        for i in range(20):
            record = logging.LogRecord("test", logging.INFO, "", 0, f"line {i}", (), None)
            buf.emit(record)

        async with TestClient(TestServer(healthz.build_app())) as client:
            resp = await client.get("/logs?tail=5")
            body = await resp.json()
        assert body["line_count"] <= 5

    @pytest.mark.asyncio
    async def test_returns_503_when_buffer_not_installed(self) -> None:
        import router.log_buffer as lb

        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h, LogBuffer)]
        lb._buffer = None

        async with TestClient(TestServer(healthz.build_app())) as client:
            resp = await client.get("/logs")
            assert resp.status == 503
            body = await resp.json()
        assert body["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_invalid_tail_param_uses_default(self, _install_fresh_buffer) -> None:
        async with TestClient(TestServer(healthz.build_app())) as client:
            resp = await client.get("/logs?tail=not-a-number")
            assert resp.status == 200
            body = await resp.json()
        assert body["status"] == "ok"
