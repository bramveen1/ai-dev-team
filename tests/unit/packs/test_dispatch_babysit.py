"""Unit tests for packs/dispatch/babysit.py — the per-dispatch watcher.

The babysit owns the actual ``claude -p`` child process while it runs,
parses its stream-json stdout, and writes state files. These tests
exercise it against real subprocesses (``echo`` and a Python one-liner)
because the contract is "tail stdout, write files" — mocking the I/O
would just test the mock.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_babysit():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test_babysit", PACK_DIR / "babysit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def babysit(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
    return _load_babysit()


def _proc_is_running(pid: int) -> bool:
    """True if *pid* is alive and not a zombie, per ``/proc/<pid>/stat``.

    Unlike ``os.kill(pid, 0)``, this treats a zombie (state ``Z``) as
    "not running" — a process that received SIGKILL but whose exit status
    hasn't been reaped yet still answers signal 0, which would otherwise
    make a successful kill look like a survivor.
    """
    try:
        content = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    state = content.rsplit(")", 1)[1].split()[0]
    return state != "Z"


class TestRun:
    def test_writes_exitcode_on_normal_exit(self, babysit, tmp_path):
        # ``true`` exits 0 without writing anything to stdout.
        rc = babysit.run(dispatch_id="d1", cmd=["true"])
        assert rc == 0
        assert (tmp_path / "d1" / "exitcode").read_text() == "0"

    def test_records_pid(self, babysit, tmp_path):
        babysit.run(dispatch_id="d1", cmd=["true"])
        pid_file = tmp_path / "d1" / "pid"
        assert pid_file.exists()
        # The pid we recorded must be the current process — the babysit
        # is the dispatch's process-group leader.
        assert int(pid_file.read_text()) == os.getpid()

    def test_parses_stream_json_events(self, babysit, tmp_path):
        # Print three JSON lines like claude -p --output-format stream-json
        # would: an assistant event, a tool_use event, and a result event
        # with a cost.
        events = [
            {"type": "assistant"},
            {"type": "tool_use", "tool_name": "Edit"},
            {"type": "result", "total_cost_usd": 0.12, "result": "PR opened: https://github.com/o/r/pull/9"},
        ]
        script = "import json,sys\nfor e in [{}]:\n    print(json.dumps(e), flush=True)".format(
            ",".join(json.dumps(e) for e in events)
        )

        rc = babysit.run(dispatch_id="d2", cmd=[sys.executable, "-c", script])
        assert rc == 0

        d = tmp_path / "d2"
        assert (d / "last_event").read_text() == "result"
        assert (d / "last_tool").read_text() == "Edit"
        assert (d / "cost").read_text() == "0.12"
        assert (d / "pr_url").read_text() == "https://github.com/o/r/pull/9"

        transcript = (d / "transcript.jsonl").read_text().splitlines()
        assert len(transcript) == 3

    def test_propagates_nonzero_exit_code(self, babysit, tmp_path):
        rc = babysit.run(dispatch_id="d3", cmd=["false"])
        assert rc == 1
        assert (tmp_path / "d3" / "exitcode").read_text() == "1"

    def test_spawn_failure_writes_synthetic_exitcode(self, babysit, tmp_path):
        rc = babysit.run(dispatch_id="d4", cmd=["definitely-not-on-path-xyz"])
        assert rc == -1
        assert (tmp_path / "d4" / "exitcode").read_text() == "-1"

    def test_malformed_json_lines_are_skipped(self, babysit, tmp_path):
        # First line malformed, second valid. last_event should reflect the second.
        script = "print('not-json', flush=True); print('{\"type\": \"valid\"}', flush=True)"
        rc = babysit.run(dispatch_id="d5", cmd=[sys.executable, "-c", script])
        assert rc == 0
        assert (tmp_path / "d5" / "last_event").read_text() == "valid"

    def test_writes_heartbeat(self, babysit, tmp_path):
        babysit.run(dispatch_id="d_hb", cmd=["true"])
        assert (tmp_path / "d_hb" / babysit.FIELD_HEARTBEAT).exists()


class TestMainCli:
    def test_main_passes_cmd_through_doubledash(self, babysit, tmp_path):
        # The handler invokes us with: babysit.py --dispatch-id <id> --cwd <cwd> -- <cmd...>
        rc = babysit.main(["--dispatch-id", "d6", "--cwd", str(tmp_path), "--", "true"])
        assert rc == 0
        assert (tmp_path / "d6" / "exitcode").read_text() == "0"

    def test_main_refuses_empty_cmd(self, babysit, tmp_path):
        rc = babysit.main(["--dispatch-id", "d7", "--cwd", str(tmp_path)])
        assert rc == 2
        assert (tmp_path / "d7" / "exitcode").read_text() == "-1"


class TestMarkerKillLadder:
    """Kill-ladder tests for _marker_poll_loop (issue #456).

    Each subprocess is spawned with start_new_session=True so os.killpg
    targets only the child's process group, never the test runner's group.
    Marker files are written under tmp_path — never /var/lib/dispatch/.
    """

    def test_sigterm_honored_no_sigkill(self, babysit, tmp_path):
        """Child that honours SIGTERM exits without SIGKILL escalation."""
        dispatch_id = "d_kl_a"
        (tmp_path / dispatch_id).mkdir()
        (tmp_path / dispatch_id / babysit.FIELD_HALT_MARKER).touch()

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(100)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            stop_event = threading.Event()
            babysit._marker_poll_loop(dispatch_id, proc, stop_event, interval=0.05, grace=3.0)

            assert proc.poll() is not None, "child should have exited after SIGTERM"
            assert proc.returncode != -9, "SIGKILL should not have been needed"
            assert stop_event.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_sigterm_ignored_escalates_to_sigkill(self, babysit, tmp_path):
        """Child that ignores SIGTERM is escalated to SIGKILL within the grace window."""
        dispatch_id = "d_kl_b"
        (tmp_path / dispatch_id).mkdir()
        (tmp_path / dispatch_id / babysit.FIELD_TIMEOUT_MARKER).touch()

        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(100)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            stop_event = threading.Event()
            babysit._marker_poll_loop(dispatch_id, proc, stop_event, interval=0.05, grace=0.3)

            # After SIGKILL is sent, wait for the OS to reap the child.
            proc.wait(timeout=5.0)
            assert proc.poll() is not None, "child should have been killed by SIGKILL"
            assert stop_event.is_set()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_grandchildren_killed_via_process_group(self, babysit, tmp_path):
        """A SIGTERM-ignoring child plus its git/gh-like grandchild are both
        gone after a halt marker (issue #379).

        Proves the kill reaches the process GROUP, not just the immediate
        pid: the child spawns a grandchild (standing in for a `git`/`gh`
        subprocess) that also ignores SIGTERM. Only os.killpg — not
        proc.terminate()/proc.kill(), which target the immediate pid only —
        can reach it.
        """
        dispatch_id = "d_kl_grandchild"
        (tmp_path / dispatch_id).mkdir()
        (tmp_path / dispatch_id / babysit.FIELD_HALT_MARKER).touch()

        child_script = (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "gc = subprocess.Popen([sys.executable, '-c',\n"
            "    'import signal, time\\n'\n"
            "    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
            "    'time.sleep(100)\\n'])\n"
            "print(gc.pid, flush=True)\n"
            "time.sleep(100)\n"
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        grandchild_pid = int(proc.stdout.readline().strip())
        try:
            stop_event = threading.Event()
            babysit._marker_poll_loop(dispatch_id, proc, stop_event, interval=0.05, grace=0.3)

            proc.wait(timeout=5.0)
            assert proc.poll() is not None, "child should have been killed by SIGKILL"

            # The grandchild inherited the child's process group (it was
            # never given its own session), so os.killpg on the group must
            # reach it too. It becomes a zombie rather than fully vanishing
            # (this test container's pid 1 doesn't reap orphans), so check
            # /proc state instead of kill(pid, 0) — a zombie still answers
            # signal 0 as "alive" even though it was in fact killed.
            deadline = time.monotonic() + 5.0
            grandchild_running = True
            while time.monotonic() < deadline:
                grandchild_running = _proc_is_running(grandchild_pid)
                if not grandchild_running:
                    break
                time.sleep(0.05)
            assert not grandchild_running, "grandchild process survived the process-group kill"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            try:
                os.kill(grandchild_pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

    def test_stop_heartbeat_set_on_marker_kill(self, babysit, tmp_path):
        """_stop_heartbeat is set after the marker kill path fires."""
        dispatch_id = "d_kl_c"
        (tmp_path / dispatch_id).mkdir()
        (tmp_path / dispatch_id / babysit.FIELD_HALT_MARKER).touch()

        class _FakeProc:
            # Use a PID beyond any valid range so os.getpgid raises
            # ProcessLookupError / OSError, exercising the proc.kill() fallback.
            pid = 4194305

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired([], timeout or 0)

        stop_event = threading.Event()
        babysit._marker_poll_loop(dispatch_id, _FakeProc(), stop_event, interval=0.05, grace=0.05)

        assert stop_event.is_set(), "_stop_heartbeat must be set on the marker-kill path"


class TestUndraftPR:
    """Tests for the deterministic un-draft-on-capture step (issue #769).

    `gh` is always mocked via `subprocess.run` — no live network/gh calls.
    """

    def _gh_call_recorder(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0)

        return calls, fake_run

    def test_undraft_invoked_once_on_first_pr_url(self, babysit, tmp_path):
        """First pr_url event in the stream triggers exactly one `gh pr ready` call."""
        calls, fake_run = self._gh_call_recorder()
        script = (
            "import json; print(json.dumps({'type': 'result', 'total_cost_usd': 0.1, "
            "'result': 'PR opened: https://github.com/o/r/pull/9'}), flush=True)"
        )
        with patch.object(babysit.subprocess, "run", fake_run):
            rc = babysit.run(dispatch_id="d_undraft_a", cmd=[sys.executable, "-c", script])

        assert rc == 0
        assert calls == [["gh", "pr", "ready", "https://github.com/o/r/pull/9"]]
        assert (tmp_path / "d_undraft_a" / babysit.FIELD_PR_READIED_MARKER).exists()

    def test_undraft_not_reinvoked_on_second_pr_url_event(self, babysit, tmp_path):
        """A second event carrying a pr_url does not re-invoke `gh pr ready` (marker honoured)."""
        calls, fake_run = self._gh_call_recorder()
        with patch.object(babysit.subprocess, "run", fake_run):
            babysit._maybe_undraft_pr("d_undraft_b", "https://github.com/o/r/pull/1")
            babysit._maybe_undraft_pr("d_undraft_b", "https://github.com/o/r/pull/1")

        assert len(calls) == 1

    def test_undraft_failure_is_swallowed(self, babysit, tmp_path):
        """A raising `gh` call does not propagate and the marker is still set (no retry)."""

        def raising_run(cmd, **kwargs):
            raise OSError("gh not found")

        with patch.object(babysit.subprocess, "run", raising_run):
            babysit._maybe_undraft_pr("d_undraft_c", "https://github.com/o/r/pull/1")  # must not raise

        assert (tmp_path / "d_undraft_c" / babysit.FIELD_PR_READIED_MARKER).exists()

    def test_undraft_failure_does_not_affect_watch_loop_or_exitcode(self, babysit, tmp_path):
        """A raising `gh` call never escapes the stdout loop and never changes the exit code."""

        def raising_run(cmd, **kwargs):
            raise OSError("gh not found")

        script = (
            "import json; print(json.dumps({'type': 'result', 'total_cost_usd': 0.1, "
            "'result': 'PR opened: https://github.com/o/r/pull/2'}), flush=True)"
        )
        with patch.object(babysit.subprocess, "run", raising_run):
            rc = babysit.run(dispatch_id="d_undraft_d", cmd=[sys.executable, "-c", script])

        assert rc == 0
        assert (tmp_path / "d_undraft_d" / "pr_url").read_text() == "https://github.com/o/r/pull/2"
        assert (tmp_path / "d_undraft_d" / "exitcode").read_text() == "0"

    def test_no_gh_call_when_no_pr_url_in_events(self, babysit, tmp_path):
        """No event carries a pr_url → `gh pr ready` is never invoked."""
        calls, fake_run = self._gh_call_recorder()
        script = "import json; print(json.dumps({'type': 'result', 'total_cost_usd': 0.1}), flush=True)"
        with patch.object(babysit.subprocess, "run", fake_run):
            rc = babysit.run(dispatch_id="d_undraft_e", cmd=[sys.executable, "-c", script])

        assert rc == 0
        assert calls == []


class TestSlackPost:
    """Tests for _slack_post token priority and silent-failure logging."""

    def _make_env(self, monkeypatch, *, workers_token=None, slack_token=None):
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        if workers_token is not None:
            monkeypatch.setenv("WORKERS_BOT_TOKEN", workers_token)
        if slack_token is not None:
            monkeypatch.setenv("SLACK_BOT_TOKEN", slack_token)

    def test_workers_bot_token_used_when_set(self, babysit, monkeypatch):
        """WORKERS_BOT_TOKEN is present → its value appears in the Authorization header."""
        self._make_env(monkeypatch, workers_token="workers-tok", slack_token="slack-tok")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return MagicMock()

        with patch.object(babysit._urlrequest, "urlopen", fake_urlopen):
            result = babysit._slack_post("C123", "ts.1", "hello")

        assert result is True
        assert captured["auth"] == "Bearer workers-tok"

    def test_slack_bot_token_fallback_when_only_it_is_set(self, babysit, monkeypatch):
        """Only SLACK_BOT_TOKEN set → falls back to it."""
        self._make_env(monkeypatch, workers_token=None, slack_token="slack-tok")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return MagicMock()

        with patch.object(babysit._urlrequest, "urlopen", fake_urlopen):
            result = babysit._slack_post("C123", "ts.1", "hello")

        assert result is True
        assert captured["auth"] == "Bearer slack-tok"

    def test_neither_token_returns_false_and_logs_warning(self, babysit, monkeypatch, caplog):
        """No token set → returns False and emits a warning."""
        self._make_env(monkeypatch, workers_token=None, slack_token=None)

        with caplog.at_level(logging.WARNING, logger="dispatch.babysit"):
            result = babysit._slack_post("C123", "ts.1", "hello")

        assert result is False
        assert any("WORKERS_BOT_TOKEN" in r.message or "skipping" in r.message.lower() for r in caplog.records)
