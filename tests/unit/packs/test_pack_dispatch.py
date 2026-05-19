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


def _no_op_seed_auth(workspace: Path) -> Path:
    """D-3 test helper: create the auth dir without actually copying credentials."""
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


# D-7: approval gate is fail-closed by default. Tests that aren't
# testing approval gating must pass this config to bypass the gate.
_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


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
        # D-7 wired dispatch_issue as an approve-gated verb so the router
        # renders Approve/Edit/Discard on its draft cards. No env-injected
        # secrets, no sidecar.
        assert pack.needs == []
        assert pack.approve == ["dispatch_issue"]
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
        rc = handler.run(["dispatch_frob"])
        assert rc != 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "unknown_verb"
        assert payload["verb"] == "dispatch_frob"


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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
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


# ── _resolve_slack_context (flags + env fallback) ────────────────────


class TestResolveSlackContext:
    def test_all_flags_present_wins(self, handler) -> None:
        ch, ts, ag = handler._resolve_slack_context(
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            environ={},
        )
        assert (ch, ts, ag) == ("C1", "1.0", "sam")

    def test_env_fallback_when_flags_missing(self, handler) -> None:
        ch, ts, ag = handler._resolve_slack_context(
            channel=None,
            thread_ts=None,
            agent=None,
            environ={
                handler.DISPATCH_CHANNEL_ENV: "C_env",
                handler.DISPATCH_THREAD_TS_ENV: "5.5",
                handler.DISPATCH_AGENT_ENV: "lisa",
            },
        )
        assert (ch, ts, ag) == ("C_env", "5.5", "lisa")

    def test_flag_beats_env(self, handler) -> None:
        """Host invocation must keep working: explicit flag wins."""
        ch, ts, ag = handler._resolve_slack_context(
            channel="C_flag",
            thread_ts="9.9",
            agent="sam",
            environ={
                handler.DISPATCH_CHANNEL_ENV: "C_env",
                handler.DISPATCH_THREAD_TS_ENV: "5.5",
                handler.DISPATCH_AGENT_ENV: "lisa",
            },
        )
        assert (ch, ts, ag) == ("C_flag", "9.9", "sam")

    def test_missing_everything_raises_with_names(self, handler) -> None:
        with pytest.raises(ValueError) as exc:
            handler._resolve_slack_context(
                channel=None,
                thread_ts=None,
                agent=None,
                environ={},
            )
        msg = str(exc.value)
        assert handler.DISPATCH_CHANNEL_ENV in msg
        assert handler.DISPATCH_THREAD_TS_ENV in msg
        assert handler.DISPATCH_AGENT_ENV in msg

    def test_partial_missing_names_only_the_missing(self, handler) -> None:
        with pytest.raises(ValueError) as exc:
            handler._resolve_slack_context(
                channel="C1",
                thread_ts=None,
                agent="sam",
                environ={},
            )
        msg = str(exc.value)
        assert handler.DISPATCH_THREAD_TS_ENV in msg
        assert handler.DISPATCH_CHANNEL_ENV not in msg
        assert handler.DISPATCH_AGENT_ENV not in msg

    def test_empty_string_flag_treated_as_unset(self, handler) -> None:
        """An empty --channel "" must not silently default to a wrong channel."""
        with pytest.raises(ValueError):
            handler._resolve_slack_context(
                channel="",
                thread_ts="1.0",
                agent="sam",
                environ={},
            )


# ── dispatch_cancel ──────────────────────────────────────────────────


class TestDispatchCancel:
    """Unit tests for the dispatch_cancel kill-ladder verb (D-4)."""

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _seed_dispatch(workspace: Path, *, pid: int | None = 12345, started_at: str | None = None) -> None:
        """Write the minimal state files a running dispatch would have."""
        workspace.mkdir(parents=True, exist_ok=True)
        if pid is not None:
            (workspace / "pid").write_text(str(pid))
        ts = started_at or "2026-05-18T09:00:00+00:00"
        (workspace / "started_at").write_text(ts)
        (workspace / "budget").write_text("1800")
        (workspace / "cost").write_text("0.42")

    @staticmethod
    def _make_kill_pg(killed: list) -> Any:
        """Return a _kill_pg_fn that records calls."""

        def fake(pgid, sig):
            killed.append((pgid, sig))
            return True

        return fake

    @staticmethod
    def _make_is_alive(alive_results: list[bool]) -> Any:
        """Return an _is_alive_fn that pops from alive_results."""
        it = iter(alive_results)

        def fake(pid):
            try:
                return next(it)
            except StopIteration:
                return False

        return fake

    # ── positive: SIGTERM sufficient ─────────────────────────────────

    def test_sigterm_kills_process_and_wipes_workspace(self, handler, tmp_path: Path) -> None:
        dispatch_id = "dispatch-20260518T090000-aabbcc"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        killed: list = []
        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.01,
            _kill_pg_fn=self._make_kill_pg(killed),
            _is_alive_fn=self._make_is_alive([False]),  # dies after SIGTERM
            _sleep_fn=lambda _: None,
        )

        assert result["status"] == "cancelled"
        assert result["dispatch_id"] == dispatch_id
        assert result["force_killed"] is False
        assert result["exitcode"] == handler.EXITCODE_SIGTERM
        # Must send SIGTERM (and only SIGTERM since process died)
        assert any(sig == 15 for _, sig in killed)
        assert all(sig != 9 for _, sig in killed)
        # Workspace must be gone.
        assert not workspace.exists()

    def test_elapsed_and_cost_in_result(self, handler, tmp_path: Path) -> None:
        dispatch_id = "dispatch-test-elapsed"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace, started_at="2026-05-18T09:00:00+00:00")

        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.01,
            _kill_pg_fn=lambda *a: True,
            _is_alive_fn=lambda _: False,
            _sleep_fn=lambda _: None,
        )

        assert result["status"] == "cancelled"
        assert "elapsed" in result
        assert result["cost"] == "0.42"

    # ── positive: SIGKILL needed ──────────────────────────────────────

    def test_sigkill_fires_when_grace_expires(self, handler, tmp_path: Path) -> None:
        dispatch_id = "dispatch-stubborn"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        killed: list = []
        # grace=0.0 → the while-loop deadline is already past on entry so the
        # loop body never runs; the post-loop alive check fires SIGKILL.
        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.0,
            _kill_pg_fn=self._make_kill_pg(killed),
            _is_alive_fn=lambda _: True,  # always alive → SIGKILL needed
            _sleep_fn=lambda _: None,
        )

        assert result["status"] == "cancelled"
        assert result["force_killed"] is True
        assert result["exitcode"] == handler.EXITCODE_SIGKILL
        sigs = [sig for _, sig in killed]
        assert 15 in sigs  # SIGTERM
        assert 9 in sigs  # SIGKILL

    # ── negative: already done ────────────────────────────────────────

    def test_completed_dispatch_returns_noop_not_running(self, handler, tmp_path: Path) -> None:
        dispatch_id = "dispatch-done"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)
        (workspace / "exitcode").write_text("0")

        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
        )

        assert result["status"] == "noop"
        assert result["reason"] == "not_running"
        # Workspace must still exist — we didn't touch it.
        assert workspace.exists()

    def test_unknown_dispatch_returns_noop_not_found(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_cancel(
            dispatch_id="dispatch-ghost",
            workspace_root=tmp_path,
        )

        assert result["status"] == "noop"
        assert result["reason"] == "not_found"

    # ── negative: queued but never started ───────────────────────────

    def test_queued_dispatch_cancelled_before_start(self, handler, tmp_path: Path) -> None:
        dispatch_id = "dispatch-queued"
        workspace = tmp_path / dispatch_id
        # Workspace created but no pid file (never got a subprocess).
        self._seed_dispatch(workspace, pid=None)

        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
        )

        assert result["status"] == "cancelled"
        assert result.get("was_queued") is True
        assert "never started" in result.get("note", "")
        # Workspace wiped.
        assert not workspace.exists()

    # ── slot release ──────────────────────────────────────────────────

    def test_cancel_releases_slot(self, handler, tmp_path: Path) -> None:
        """dispatch_cancel must remove the slot file so the pool is restored."""
        dispatch_id = "dispatch-20260519T000000-slot01"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        # Simulate a held slot: write slot file with dispatch_id as body.
        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        slots_dir.mkdir()
        slot_file = slots_dir / "slot-0"
        slot_file.write_text(dispatch_id)

        handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.0,
            _kill_pg_fn=lambda *a: True,
            _is_alive_fn=lambda _: False,
            _sleep_fn=lambda _: None,
        )

        assert not slot_file.exists(), "slot file must be removed after cancel"

    def test_cancel_slot_release_idempotent_when_no_slot(self, handler, tmp_path: Path) -> None:
        """dispatch_cancel must not error when no slot file is present."""
        dispatch_id = "dispatch-20260519T000000-noslot"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        # No slot file created — simulates janitor already cleaned it up.
        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        slots_dir.mkdir()

        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.0,
            _kill_pg_fn=lambda *a: True,
            _is_alive_fn=lambda _: False,
            _sleep_fn=lambda _: None,
        )

        assert result["status"] == "cancelled"

    # ── CLI wiring ────────────────────────────────────────────────────

    def test_cli_dispatch_cancel_runs_through_run(
        self,
        handler,
        tmp_path: Path,
        capsys: "pytest.CaptureFixture[str]",
        monkeypatch,
    ) -> None:
        recorded: dict = {}

        def fake_cancel(**kwargs):
            recorded.update(kwargs)
            return {"status": "cancelled", "dispatch_id": kwargs["dispatch_id"]}

        monkeypatch.setattr(handler, "dispatch_cancel", fake_cancel)

        rc = handler.run(["dispatch_cancel", "--dispatch-id", "dispatch-abc123"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "cancelled"
        assert recorded["dispatch_id"] == "dispatch-abc123"

    def test_cli_noop_still_exits_zero(
        self,
        handler,
        tmp_path: Path,
        capsys: "pytest.CaptureFixture[str]",
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            handler,
            "dispatch_cancel",
            lambda **kw: {"status": "noop", "reason": "not_running", "dispatch_id": kw["dispatch_id"]},
        )

        rc = handler.run(["dispatch_cancel", "--dispatch-id", "dispatch-done"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "noop"

    def test_dispatch_cancel_now_known_verb(
        self,
        handler,
        capsys: "pytest.CaptureFixture[str]",
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """dispatch_cancel must no longer appear in the unknown_verb message."""
        monkeypatch.setattr(
            handler,
            "dispatch_cancel",
            lambda **kw: {"status": "noop", "reason": "not_found", "dispatch_id": kw["dispatch_id"]},
        )
        rc = handler.run(["dispatch_cancel", "--dispatch-id", "d-1"])
        capsys.readouterr()  # drain first call's output
        assert rc == 0

        # An actually-unknown verb — the message must list dispatch_cancel as known.
        assert handler.run(["dispatch_frob"]) == handler.EXIT_USAGE
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "unknown_verb"
        msg = payload.get("message", "")
        # dispatch_cancel is now a real verb, so it appears in the known-verbs list.
        assert "dispatch_cancel" in msg
        # The old "lands in their own issues" placeholder must be gone.
        assert "land in their own issues" not in msg


class TestSupervisionWorkspaceGone:
    """Regression: supervision must deregister cleanly when workspace was wiped by dispatch_cancel."""

    @pytest.mark.asyncio
    async def test_workspace_gone_returns_done(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from router.dispatch import supervision

        slack = MagicMock()
        slack.chat_postMessage = AsyncMock()

        # The dispatch dir simply does not exist (wiped by dispatch_cancel).
        result = await supervision.check_dispatch(
            payload={"dispatch_id": "dispatch-wiped", "channel": "C1", "thread_ts": "1.0", "agent": "sam"},
            slack_client=slack,
            dispatch_root=str(tmp_path),
        )

        assert result == {"status": "done", "reason": "workspace_gone"}
        # No Slack message — cancel notification already went via Sam's response.
        slack.chat_postMessage.assert_not_awaited()


class TestCliEnvFallback:
    """End-to-end: CLI without --channel/--thread-ts/--agent reads env."""

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_cli_uses_env_when_flags_omitted(self, handler, tmp_path: Path, capsys, monkeypatch) -> None:
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "d-1", "workspace": str(tmp_path), "pid": 1}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)
        monkeypatch.setenv(handler.DISPATCH_CHANNEL_ENV, "C_env")
        monkeypatch.setenv(handler.DISPATCH_THREAD_TS_ENV, "7.7")
        monkeypatch.setenv(handler.DISPATCH_AGENT_ENV, "sam")

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/9",
            ]
        )
        assert rc == 0
        assert recorded["channel"] == "C_env"
        assert recorded["thread_ts"] == "7.7"
        assert recorded["agent"] == "sam"

    def test_cli_errors_clearly_when_neither_flags_nor_env(self, handler, capsys, monkeypatch) -> None:
        monkeypatch.delenv(handler.DISPATCH_CHANNEL_ENV, raising=False)
        monkeypatch.delenv(handler.DISPATCH_THREAD_TS_ENV, raising=False)
        monkeypatch.delenv(handler.DISPATCH_AGENT_ENV, raising=False)

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/9",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "missing_slack_context"
        # Message must name each missing variable so the operator can fix it.
        assert handler.DISPATCH_CHANNEL_ENV in payload["message"]
        assert handler.DISPATCH_THREAD_TS_ENV in payload["message"]
        assert handler.DISPATCH_AGENT_ENV in payload["message"]


# ── D-5: dispatch_health quota fields ────────────────────────────────────────


class TestDispatchHealthQuota:
    """Quota telemetry fields added to dispatch_health output."""

    def test_quota_fields_present_in_health(self, handler, tmp_path: Path) -> None:
        version = _completed(stdout="2.1.142\n")
        probe = _completed(stdout=json.dumps({"is_error": False, "result": "hello"}))

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=tmp_path,
        )
        assert "window_cost_usd" in result
        assert "dispatches_this_window" in result
        assert "quota_locked" in result
        # Empty workspace → no locked, zero cost, zero dispatches.
        assert result["window_cost_usd"] == 0.0
        assert result["dispatches_this_window"] == 0
        assert result["quota_locked"] is False
        assert "quota_retry_after" not in result

    def test_quota_locked_field_when_sentinel_present(self, handler, tmp_path: Path) -> None:
        from datetime import datetime, timedelta, timezone

        # Write a lock sentinel that's less than 5h old.
        locked_at = datetime.now(timezone.utc) - timedelta(hours=1)
        (tmp_path / ".quota_locked").write_text(locked_at.isoformat())

        version = _completed(stdout="2.1.142\n")
        probe = _completed(stdout=json.dumps({"is_error": False, "result": "hello"}))

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=_make_run(version, probe),
            workspace_root=tmp_path,
        )
        assert result["quota_locked"] is True
        assert "quota_retry_after" in result


# ── D-5: dispatch_issue quota_locked short-circuit ───────────────────────────


class TestDispatchIssueQuotaLocked:
    """dispatch_issue returns quota_locked error without spawning claude."""

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_quota_locked_returns_error_without_spawning(self, handler, tmp_path: Path) -> None:
        from datetime import datetime, timedelta, timezone

        # Write a fresh lock sentinel.
        locked_at = datetime.now(timezone.utc) - timedelta(hours=1)
        (tmp_path / ".quota_locked").write_text(locked_at.isoformat())

        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            exec_override=["sleep", "1"],
            _approval_cfg=_NO_GATE_CFG,
        )

        assert result["status"] == "error"
        assert result["reason"] == "quota_locked"
        assert "retry_after" in result
        # No subprocess should have been spawned.
        assert len(_FakePopen.instances) == 0

    def test_quota_lock_clears_after_window(self, handler, tmp_path: Path) -> None:
        from datetime import datetime, timedelta, timezone

        # Write an expired lock sentinel (6h ago, window=5h).
        locked_at = datetime.now(timezone.utc) - timedelta(hours=6)
        (tmp_path / ".quota_locked").write_text(locked_at.isoformat())

        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            exec_override=["sleep", "1"],
            _approval_cfg=_NO_GATE_CFG,
        )

        # Expired lock → dispatch proceeds normally.
        assert result["status"] in ("launched", "completed")
        assert result.get("reason") != "quota_locked"
