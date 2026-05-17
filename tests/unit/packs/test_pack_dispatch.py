"""Unit tests for the dispatch pack scaffold (#D-1).

Covers:

- Pack manifest loads cleanly and matches the D-1 contract (no needs,
  no approval verbs, no sidecar).
- Companion files (handler.py, prompt.md, README.md) exist and the
  prompt + README mention the planned verbs.
- ``dispatch_health`` happy path: mocked subprocess returns the four
  required fields true.
- Missing workspace volume → ``workspace_volume_writable: false``, no
  exception raised (acceptance criterion).
- Failing Sonnet probe → ``sonnet_probe_ok: false`` with the CLI exit
  code surfaced (acceptance criterion).
- README documents the four planned verbs + smoke check (acceptance
  criterion).

The actual ``claude`` CLI is mocked — these are unit tests, not
integration tests. The end-to-end smoke check runs in Sam's container
per the design doc.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from router.packs.loader import load_pack

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    """Import packs/dispatch/handler.py without polluting sys.modules globally."""
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


def _make_run(version_completed: Any, probe_completed: Any):
    """Build a fake subprocess.run that dispatches on argv[1:2]."""

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if len(argv) >= 2 and argv[1] == "--version":
            return version_completed
        if "-p" in argv:
            return probe_completed
        raise AssertionError(f"unexpected argv: {argv!r}")

    return fake_run


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    completed = MagicMock()
    completed.stdout = stdout
    completed.stderr = stderr
    completed.returncode = returncode
    return completed


# ── Pack-shape guards ────────────────────────────────────────────────


class TestPackShape:
    def test_manifest_loads_cleanly(self) -> None:
        pack = load_pack(PACK_DIR)
        assert pack.name == "dispatch"
        # D-1 scaffold: no env-injected secrets, no approval verbs yet
        # (D-7 wires that), no sidecar.
        assert pack.needs == []
        assert pack.approve == []
        assert pack.requires_sidecar is False
        assert pack.description.strip()

    def test_companion_files_exist(self) -> None:
        pack = load_pack(PACK_DIR)
        assert pack.prompt_path is not None
        assert (PACK_DIR / "handler.py").exists()
        assert (PACK_DIR / "README.md").exists()

    def test_prompt_lists_planned_verbs(self) -> None:
        text = (PACK_DIR / "prompt.md").read_text()
        for verb in ("dispatch_health", "dispatch_issue", "dispatch_status", "dispatch_cancel"):
            assert verb in text, f"prompt.md must mention {verb}"

    def test_readme_lists_verbs_and_smoke_check(self) -> None:
        text = (PACK_DIR / "README.md").read_text()
        for verb in ("dispatch_health", "dispatch_issue", "dispatch_status", "dispatch_cancel"):
            assert verb in text, f"README.md must mention {verb}"
        # Smoke check must be copy-pasteable from the design doc.
        assert "docker compose up -d --force-recreate sam" in text
        assert "dispatch_health" in text


# ── dispatch_health ──────────────────────────────────────────────────


class TestDispatchHealthHappyPath:
    def test_all_four_fields_true(self, handler, tmp_path: Path) -> None:
        version = _completed(stdout="2.1.142 (Claude Code)\n", returncode=0)
        probe = _completed(
            stdout=json.dumps({"is_error": False, "result": "hello there"}),
            returncode=0,
        )
        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=tmp_path,
        )
        assert result["cli_version"] == "2.1.142 (Claude Code)"
        assert result["claude_path"] == "/usr/local/bin/claude"
        assert result["workspace_volume_writable"] is True
        assert result["sonnet_probe_ok"] is True
        assert result["sonnet_probe_exit_code"] == 0
        # On the happy path there's no detail field.
        assert "sonnet_probe_detail" not in result

    def test_uses_sonnet_model_and_json_output_format(self, handler, tmp_path: Path) -> None:
        """Health probe must pin the model to Sonnet so it never burns Opus quota."""
        seen_argvs: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            seen_argvs.append(list(argv))
            if len(argv) >= 2 and argv[1] == "--version":
                return _completed(stdout="2.1.142\n")
            return _completed(stdout=json.dumps({"is_error": False, "result": "hello"}))

        handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=fake_run,
            workspace_root=tmp_path,
        )

        probe_argvs = [a for a in seen_argvs if "-p" in a]
        assert len(probe_argvs) == 1, f"expected one probe call, got {seen_argvs!r}"
        argv = probe_argvs[0]
        assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
        assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"


# ── Acceptance: missing workspace volume ─────────────────────────────


class TestDispatchHealthMissingVolume:
    def test_missing_root_dir_returns_false_no_exception(self, handler, tmp_path: Path) -> None:
        missing = tmp_path / "definitely-not-mounted"
        assert not missing.exists()

        version = _completed(stdout="2.1.142\n")
        probe = _completed(stdout=json.dumps({"is_error": False, "result": "hello"}))

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=missing,
        )
        assert result["workspace_volume_writable"] is False
        # Other fields still populate — the missing volume doesn't poison
        # the rest of the report.
        assert result["claude_path"] == "/usr/local/bin/claude"
        assert result["sonnet_probe_ok"] is True

    def test_unwritable_root_dir_returns_false_no_exception(
        self,
        handler,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A volume that exists but rejects writes must also surface as False."""
        root = tmp_path / "ro-root"
        root.mkdir()

        # Force write_text to raise OSError (PermissionError is a subclass).
        original_write_text = Path.write_text

        def boom(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.parent == root:
                raise PermissionError(f"read-only volume: {self}")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", boom)

        version = _completed(stdout="2.1.142\n")
        probe = _completed(stdout=json.dumps({"is_error": False, "result": "hello"}))

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=root,
        )
        assert result["workspace_volume_writable"] is False


# ── Acceptance: failing Sonnet probe ─────────────────────────────────


class TestDispatchHealthSonnetFailure:
    def test_nonzero_exit_surfaces_exit_code_no_exception(self, handler, tmp_path: Path) -> None:
        version = _completed(stdout="2.1.142\n")
        probe = _completed(stdout="", stderr="quota exhausted", returncode=42)

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=tmp_path,
        )
        assert result["sonnet_probe_ok"] is False
        assert result["sonnet_probe_exit_code"] == 42
        assert "sonnet_probe_detail" in result
        assert "quota exhausted" in result["sonnet_probe_detail"]
        # Other fields still populate.
        assert result["workspace_volume_writable"] is True
        assert result["cli_version"] == "2.1.142"

    def test_is_error_true_in_envelope_surfaces_as_false(self, handler, tmp_path: Path) -> None:
        version = _completed(stdout="2.1.142\n")
        probe = _completed(
            stdout=json.dumps({"is_error": True, "result": "auth failed"}),
            returncode=0,
        )

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=tmp_path,
        )
        assert result["sonnet_probe_ok"] is False
        assert result["sonnet_probe_exit_code"] == 0
        assert "sonnet_probe_detail" in result

    def test_timeout_surfaces_as_false(self, handler, tmp_path: Path) -> None:
        version = _completed(stdout="2.1.142\n")

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            if len(argv) >= 2 and argv[1] == "--version":
                return version
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=fake_run,
            workspace_root=tmp_path,
        )
        assert result["sonnet_probe_ok"] is False
        assert result["sonnet_probe_exit_code"] is None
        assert "timed out" in result["sonnet_probe_detail"]

    def test_missing_claude_binary_surfaces_as_false(self, handler, tmp_path: Path) -> None:
        """If shutil.which returns None, every CLI-dependent field is empty,
        but the call still returns a structured dict."""
        result = handler.dispatch_health(
            which=lambda _: None,
            run=_make_run(_completed(), _completed()),
            workspace_root=tmp_path,
        )
        assert result["claude_path"] is None
        assert result["cli_version"] is None
        assert result["sonnet_probe_ok"] is False
        assert result["workspace_volume_writable"] is True


# ── CLI entry point ──────────────────────────────────────────────────


class TestRunCli:
    def test_unknown_verb_exits_nonzero_and_prints_json(
        self,
        handler,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # dispatch_status / dispatch_cancel land in their own issues
        # (D-2 / D-4) — they're still unknown verbs in this scaffold.
        rc = handler.run(["dispatch_status"])
        assert rc != 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "unknown_verb"
        assert payload["verb"] == "dispatch_status"


# ── dispatch_issue ───────────────────────────────────────────────────


class _FakePopen:
    """Captures Popen arguments without actually spawning a subprocess.

    ``wait_returncode`` makes the inline-mode tests deterministic — the
    fake babysit "exits" with the configured code as soon as the handler
    awaits .wait().
    """

    instances: list["_FakePopen"] = []
    wait_returncode = 0
    wait_writes_exitcode = True

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 12345
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        # Mimic the real babysit: write exitcode before returning so
        # inline-mode's "block until exitcode lands" guarantee holds.
        if self.wait_writes_exitcode and self.cwd:
            (Path(self.cwd) / "exitcode").write_text(str(self.wait_returncode))
        return self.wait_returncode

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.wait_returncode = 0
        cls.wait_writes_exitcode = True


class TestDispatchIssuePoll:
    """Poll mode — detached babysit, returns {status: launched} immediately."""

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_returns_launched_and_writes_init_state(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            budget_seconds=600,
            model="sonnet",
            persona="dev",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
        )

        assert result["status"] == "launched"
        assert result["dispatch_id"].startswith("dispatch-")
        assert result["pid"] == 12345
        assert result["budget_seconds"] == 600
        assert result["model"] == "sonnet"
        assert result["supervision_mode"] == "poll"

        workspace = Path(result["workspace"])
        assert workspace.is_dir()
        assert (workspace / "started_at").read_text()
        assert (workspace / "budget").read_text() == "600"
        assert (workspace / "channel").read_text() == "C123"
        assert (workspace / "thread_ts").read_text() == "1.0"
        assert (workspace / "agent").read_text() == "sam"
        assert (workspace / "issue_url").read_text() == "https://github.com/o/r/issues/42"
        assert (workspace / "model").read_text() == "sonnet"
        assert (workspace / "persona").read_text() == "dev"
        assert (workspace / "pid").read_text() == "12345"
        # No exitcode in poll mode — that's the babysit's job once it
        # finishes, asynchronously after the handler returns.
        assert not (workspace / "exitcode").exists()

    def test_spawns_babysit_detached(self, handler, tmp_path: Path) -> None:
        handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
        )
        assert len(_FakePopen.instances) == 1
        popen = _FakePopen.instances[0]
        # The babysit must run detached so it survives the handler exit.
        assert popen.kwargs["start_new_session"] is True
        # And it must not inherit stdio that could block on a closed pipe.
        assert popen.kwargs["stdin"] is subprocess.DEVNULL
        assert popen.kwargs["stdout"] is subprocess.DEVNULL
        assert popen.kwargs["stderr"] is subprocess.DEVNULL

    def test_exec_override_replaces_claude_command(self, handler, tmp_path: Path) -> None:
        handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            exec_override=["sleep", "30"],
            supervision_mode="poll",
        )
        argv = _FakePopen.instances[0].argv
        # ['python', babysit, '--dispatch-id', <id>, '--cwd', <cwd>, '--', 'sleep', '30']
        assert argv[-2:] == ["sleep", "30"]
        assert "--" in argv
        assert "--dispatch-id" in argv

    def test_default_exec_uses_claude_p_with_sonnet(self, handler, tmp_path: Path) -> None:
        handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
        )
        argv = _FakePopen.instances[0].argv
        # Everything after `--` is the child command.
        child = argv[argv.index("--") + 1 :]
        assert child[0] == "claude"
        assert "-p" in child
        assert "--model" in child
        assert child[child.index("--model") + 1] == "sonnet"
        assert "--output-format" in child
        assert child[child.index("--output-format") + 1] == "stream-json"


class TestDispatchIssueInline:
    """Inline mode (default) — blocks on babysit, returns full terminal envelope."""

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_inline_is_default_when_env_unset(self, handler, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("DISPATCH_SUPERVISION", raising=False)
        _FakePopen.wait_returncode = 0
        result = handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
        )
        # Default is inline: handler waits for exitcode, returns the
        # terminal envelope inline, never detaches.
        assert result["status"] == "completed"
        assert result["supervision_mode"] == "inline"
        assert result["exitcode"] == 0
        popen = _FakePopen.instances[0]
        assert "start_new_session" not in popen.kwargs

    def test_inline_failed_returncode_marks_failed(self, handler, tmp_path: Path) -> None:
        _FakePopen.wait_returncode = 2
        result = handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="inline",
        )
        assert result["status"] == "failed"
        assert result["exitcode"] == 2

    def test_env_var_flip_to_poll(self, handler, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_SUPERVISION", "poll")
        result = handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
        )
        assert result["status"] == "launched"
        assert result["supervision_mode"] == "poll"

    def test_unknown_supervision_mode_falls_back_to_inline(self, handler, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_SUPERVISION", "pol")  # typo
        result = handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
        )
        assert result["supervision_mode"] == "inline"


# Tests that don't depend on a specific supervision mode.
class TestDispatchIssue:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_launch_failure_records_synthetic_exitcode(self, handler, tmp_path: Path) -> None:
        def failing_popen(*args, **kwargs):
            raise OSError("could not spawn")

        result = handler.dispatch_issue(
            issue_url="https://example.com/i",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=failing_popen,
        )

        assert result["status"] == "launch_failed"
        workspace = Path(result["workspace"])
        # Supervisor checking this dir on its next tick must see terminal
        # state, not a half-launched orphan.
        assert (workspace / "exitcode").read_text() == "-1"

    def test_cli_dispatch_issue_runs_through_run(self, handler, tmp_path: Path, capsys, monkeypatch) -> None:
        # Stub dispatch_issue at the handler-module level so the CLI
        # wrapper is exercised end-to-end without spawning any real
        # processes.
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "disp-fixed", "workspace": str(tmp_path), "pid": 42}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/1",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--agent",
                "sam",
                "--budget-seconds",
                "600",
                "--model",
                "sonnet",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "launched"
        assert payload["dispatch_id"] == "disp-fixed"
        # The CLI properly forwarded every flag to dispatch_issue.
        assert recorded["issue_url"] == "https://github.com/o/r/issues/1"
        assert recorded["channel"] == "C1"
        assert recorded["thread_ts"] == "1.0"
        assert recorded["agent"] == "sam"
        assert recorded["budget_seconds"] == 600
        assert recorded["model"] == "sonnet"
