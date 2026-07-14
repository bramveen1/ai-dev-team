"""Unit tests for packs/dispatch/gh_cli.py (#736).

Covers run_gh/run_gh_json's uniform result for: success, non-zero
returncode, FileNotFoundError (gh missing), TimeoutExpired, OSError, and
malformed JSON — the centralized replacement for seven independently
hand-rolled "shell out to gh, catch spawn/timeout exceptions, check
returncode, slice an error tail, json.loads the stdout" call sites in
pr_review.py and handler.py.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_gh_cli():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test_gh_cli", PACK_DIR / "gh_cli.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gh_cli():
    return _load_gh_cli()


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ── run_gh: success / non-zero returncode ──────────────────────────────────


class TestRunGhSuccess:
    def test_success_returns_ok_result(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stdout="hello\n")  # noqa: E731
        result = gh_cli.run_gh(["gh", "api", "user"], run=fake_run)
        assert result.ok is True
        assert result.returncode == 0
        assert result.stdout == "hello\n"
        assert result.error is None
        assert result.error_kind is None

    def test_none_stdout_stderr_coerced_to_empty_string(self, gh_cli) -> None:
        """gh mocks sometimes leave stdout/stderr as None; never propagate that."""
        fake_run = lambda cmd, **kw: SimpleNamespace(stdout=None, stderr=None, returncode=0)  # noqa: E731
        result = gh_cli.run_gh(["gh", "api", "user"], run=fake_run)
        assert result.stdout == ""
        assert result.stderr == ""

    def test_passes_cmd_and_kwargs_through(self, gh_cli) -> None:
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["kw"] = kw
            return _completed()

        gh_cli.run_gh(["gh", "pr", "view", "1"], timeout=15, run=fake_run, env={"FOO": "bar"})
        assert seen["cmd"] == ["gh", "pr", "view", "1"]
        assert seen["kw"]["timeout"] == 15
        assert seen["kw"]["env"] == {"FOO": "bar"}
        assert seen["kw"]["capture_output"] is True
        assert seen["kw"]["text"] is True
        assert seen["kw"]["check"] is False


class TestRunGhNonZeroReturncode:
    def test_nonzero_returncode_is_not_ok(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stderr="Bad credentials", returncode=1)  # noqa: E731
        result = gh_cli.run_gh(["gh", "api", "user"], run=fake_run)
        assert result.ok is False
        assert result.returncode == 1
        assert result.error is None
        assert result.error_kind is None

    def test_error_detail_prefers_stderr_then_stdout(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(  # noqa: E731
            stdout="stdout text", stderr="stderr text", returncode=1
        )
        result = gh_cli.run_gh(["gh", "api", "user"], run=fake_run)
        assert result.error_detail() == "stderr text"

    def test_error_detail_falls_back_to_stdout_when_stderr_empty(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stdout="stdout only", returncode=1)  # noqa: E731
        result = gh_cli.run_gh(["gh", "api", "user"], run=fake_run)
        assert result.error_detail() == "stdout only"

    def test_error_detail_tail_slices(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stderr="x" * 500, returncode=1)  # noqa: E731
        result = gh_cli.run_gh(["gh", "api", "user"], run=fake_run)
        assert len(result.error_detail(tail=50)) == 50


# ── run_gh: spawn/timeout exceptions ────────────────────────────────────────


class TestRunGhSpawnFailures:
    def test_file_not_found_yields_not_found_error_kind(self, gh_cli) -> None:
        def boom(cmd, **kw):
            raise FileNotFoundError("gh not found")

        result = gh_cli.run_gh(["gh", "api", "user"], run=boom)
        assert result.ok is False
        assert result.error_kind == "not_found"
        assert result.error == "gh binary not found"

    def test_timeout_expired_yields_timeout_error_kind(self, gh_cli) -> None:
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        result = gh_cli.run_gh(["gh", "api", "user"], run=boom)
        assert result.ok is False
        assert result.error_kind == "timeout"
        assert "timed out" in result.error

    def test_os_error_yields_os_error_error_kind(self, gh_cli) -> None:
        def boom(cmd, **kw):
            raise OSError("permission denied")

        result = gh_cli.run_gh(["gh", "api", "user"], run=boom)
        assert result.ok is False
        assert result.error_kind == "os_error"
        assert result.error == "permission denied"

    def test_error_detail_returns_synthetic_message_for_spawn_failures(self, gh_cli) -> None:
        def boom(cmd, **kw):
            raise FileNotFoundError("gh not found")

        result = gh_cli.run_gh(["gh", "api", "user"], run=boom)
        assert result.error_detail() == "gh binary not found"

    def test_default_run_resolved_at_call_time_for_monkeypatching(self, gh_cli, monkeypatch) -> None:
        """``run=None`` must look up subprocess.run lazily so global monkeypatches apply."""

        def fake_run(cmd, **kw):
            return _completed(stdout="patched\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = gh_cli.run_gh(["gh", "api", "user"])
        assert result.stdout == "patched\n"


# ── run_gh_json ──────────────────────────────────────────────────────────────


class TestRunGhJson:
    def test_valid_json_parsed(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stdout='{"login": "sam"}')  # noqa: E731
        result, data = gh_cli.run_gh_json(["gh", "api", "user"], run=fake_run)
        assert result.ok is True
        assert data == {"login": "sam"}

    def test_malformed_json_returns_none_data(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stdout="not-json")  # noqa: E731
        result, data = gh_cli.run_gh_json(["gh", "api", "user"], run=fake_run)
        assert result.ok is True  # the gh call itself succeeded
        assert data is None

    def test_empty_stdout_defaults_to_empty_object(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stdout="")  # noqa: E731
        result, data = gh_cli.run_gh_json(["gh", "api", "user"], run=fake_run)
        assert data == {}

    def test_failed_run_returns_none_data_without_parsing(self, gh_cli) -> None:
        fake_run = lambda cmd, **kw: _completed(stderr="Bad credentials", returncode=1)  # noqa: E731
        result, data = gh_cli.run_gh_json(["gh", "api", "user"], run=fake_run)
        assert result.ok is False
        assert data is None

    def test_spawn_failure_returns_none_data(self, gh_cli) -> None:
        def boom(cmd, **kw):
            raise FileNotFoundError("gh not found")

        result, data = gh_cli.run_gh_json(["gh", "api", "user"], run=boom)
        assert result.error_kind == "not_found"
        assert data is None
