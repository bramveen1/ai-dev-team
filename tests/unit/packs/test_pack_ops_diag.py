"""Unit tests for the ops-diag pack (issue #218).

Covers:

- Pack manifest loads cleanly (name, description, needs, approve).
- Companion files (handler.py, prompt.md, README.md) exist and mention
  the planned verb.
- ``router_logs`` verb: happy path — mocked HTTP returns log lines.
- ``router_logs`` verb: router unavailable — returns error dict without
  raising.
- ``router_logs`` verb: bad JSON from router — returns error dict without
  raising.
"""

from __future__ import annotations

import importlib.util
import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from router.packs.loader import load_pack

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "ops-diag"


def _load_handler():
    """Import packs/ops-diag/handler.py without polluting sys.modules globally."""
    spec = importlib.util.spec_from_file_location(
        "_test_ops_diag_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Pack-shape guards ────────────────────────────────────────────────


class TestPackShape:
    def test_manifest_loads_cleanly(self) -> None:
        pack = load_pack(PACK_DIR)
        assert pack.name == "ops-diag"
        assert pack.needs == []
        assert pack.approve == []
        assert pack.description.strip()

    def test_companion_files_exist(self) -> None:
        assert (PACK_DIR / "handler.py").exists()
        assert (PACK_DIR / "prompt.md").exists()
        assert (PACK_DIR / "README.md").exists()

    def test_prompt_mentions_verb_and_flags(self) -> None:
        text = (PACK_DIR / "prompt.md").read_text()
        assert "router_logs" in text
        assert "--tail" in text
        assert "--max-bytes" in text

    def test_readme_documents_verb_and_limits(self) -> None:
        text = (PACK_DIR / "README.md").read_text()
        assert "router_logs" in text
        assert "200" in text  # default tail
        assert "500" in text  # max tail
        assert "redact" in text.lower()


# ── Handler verb: router_logs ────────────────────────────────────────


class TestRouterLogsVerb:
    def _fake_urlopen(self, url: str, *, body: str, status: int = 200):
        """Return a context-manager mock that yields a response with *body*."""
        resp = MagicMock()
        resp.read.return_value = body.encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_happy_path_returns_lines(self) -> None:
        handler = _load_handler()
        payload = json.dumps({"status": "ok", "lines": ["line1", "line2"], "line_count": 2})
        resp_mock = self._fake_urlopen("/logs", body=payload)
        with patch.object(handler._urlrequest, "urlopen", return_value=resp_mock):
            result = handler.router_logs(tail=10, max_bytes=1000, router_url="http://router:8080")
        assert result["status"] == "ok"
        assert result["lines"] == ["line1", "line2"]
        assert result["line_count"] == 2

    def test_network_error_returns_error_dict(self) -> None:
        from urllib.error import URLError

        handler = _load_handler()
        with patch.object(handler._urlrequest, "urlopen", side_effect=URLError("connection refused")):
            result = handler.router_logs(tail=10, max_bytes=1000, router_url="http://router:8080")
        assert result["status"] == "error"
        assert "connection refused" in result["error"]
        assert result["lines"] == []

    def test_invalid_json_returns_error_dict(self) -> None:
        handler = _load_handler()
        resp_mock = self._fake_urlopen("/logs", body="not-json")
        with patch.object(handler._urlrequest, "urlopen", return_value=resp_mock):
            result = handler.router_logs(tail=10, max_bytes=1000, router_url="http://router:8080")
        assert result["status"] == "error"
        assert "invalid JSON" in result["error"]

    def test_unavailable_status_passes_through(self) -> None:
        handler = _load_handler()
        payload = json.dumps({"status": "unavailable", "lines": [], "line_count": 0})
        resp_mock = self._fake_urlopen("/logs", body=payload)
        with patch.object(handler._urlrequest, "urlopen", return_value=resp_mock):
            result = handler.router_logs(tail=10, max_bytes=1000, router_url="http://router:8080")
        assert result["status"] == "unavailable"
        assert result["lines"] == []

    def test_run_router_logs_exits_zero_on_success(self) -> None:
        handler = _load_handler()
        payload = json.dumps({"status": "ok", "lines": [], "line_count": 0})
        resp_mock = self._fake_urlopen("/logs", body=payload)
        with (
            patch.object(handler._urlrequest, "urlopen", return_value=resp_mock),
            patch("sys.argv", ["handler.py", "router_logs", "--tail", "5"]),
            patch("sys.stdout", new_callable=StringIO) as mock_stdout,
        ):
            code = handler.run()
        assert code == handler.EXIT_OK
        out = json.loads(mock_stdout.getvalue())
        assert out["status"] == "ok"

    def test_run_unknown_verb_exits_usage(self) -> None:
        handler = _load_handler()
        with (
            patch("sys.argv", ["handler.py", "nonexistent_verb"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            code = handler.run()
        assert code == handler.EXIT_USAGE

    def test_run_no_verb_exits_usage(self) -> None:
        handler = _load_handler()
        with (
            patch("sys.argv", ["handler.py"]),
            patch("sys.stdout", new_callable=StringIO),
        ):
            code = handler.run()
        assert code == handler.EXIT_USAGE

    def test_router_url_env_override(self, monkeypatch) -> None:
        handler = _load_handler()
        monkeypatch.setenv("ROUTER_LOGS_URL", "http://myrouter:9000")
        assert handler._router_base_url() == "http://myrouter:9000"

    def test_router_url_default(self, monkeypatch) -> None:
        handler = _load_handler()
        monkeypatch.delenv("ROUTER_LOGS_URL", raising=False)
        assert handler._router_base_url() == handler.DEFAULT_ROUTER_URL

    def test_timeout_error_returns_error_dict(self) -> None:
        handler = _load_handler()
        with patch.object(handler._urlrequest, "urlopen", side_effect=TimeoutError("timed out")):
            result = handler.router_logs(tail=10, max_bytes=1000, router_url="http://router:8080")
        assert result["status"] == "error"
        assert result["lines"] == []

    def test_oserror_returns_error_dict(self) -> None:
        handler = _load_handler()
        with patch.object(handler._urlrequest, "urlopen", side_effect=OSError("network unreachable")):
            result = handler.router_logs(tail=10, max_bytes=1000, router_url="http://router:8080")
        assert result["status"] == "error"
        assert "network unreachable" in result["error"]
        assert result["lines"] == []
