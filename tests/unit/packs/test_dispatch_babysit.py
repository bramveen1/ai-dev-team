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
import os
import subprocess
import sys
import threading
from pathlib import Path

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
