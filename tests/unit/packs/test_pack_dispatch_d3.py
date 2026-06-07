"""Unit tests for D-3: N=3 slot pool, FIFO queue, per-dispatch CLAUDE_CONFIG_DIR.

Covers:

- Slot pool: up to POOL_SIZE concurrent dispatches each hold a slot file;
  the N+1-th dispatch queues and waits for a slot to free.
- FIFO ordering: queue tickets are sorted by creation timestamp, so the
  first-in waiter gets the slot when one opens up.
- Auth seeding: _seed_auth_dir copies canonical creds; fails fast on error;
  sets CLAUDE_CONFIG_DIR in the babysit environment.
- dispatch_status verb: reports running and queued dispatch IDs.
- Slot release: crashing babysit (finally block) and launch_failed both
  release the slot so the queue never stalls.
- No Slack token: queue/slot Slack messages silently skip when token absent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_d3_handler",
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


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


def _failing_seed_auth(workspace: Path) -> Path:
    raise RuntimeError("auth_seed_failed: disk full")


# D-7: approval gate is fail-closed. Non-D7 tests bypass it.
_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


class _FakePopen:
    """Minimal Popen stub; wait() writes exitcode=0 to cwd."""

    instances: list["_FakePopen"] = []
    wait_returncode = 0
    wait_writes_exitcode = True

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 42000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.wait_writes_exitcode and self.cwd:
            (Path(self.cwd) / "exitcode").write_text(str(self.wait_returncode))
        return self.wait_returncode

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.wait_returncode = 0
        cls.wait_writes_exitcode = True


# ── Auth seeding ─────────────────────────────────────────────────────────


class TestAuthSeed:
    def test_seed_creates_auth_dir(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        # Use a fake copytree that records calls without doing real I/O.
        copied: list = []

        def fake_copytree(src, dst, **kwargs):
            copied.append((src, dst))

        auth_dir = handler._seed_auth_dir(workspace, copy_fn=fake_copytree)
        assert auth_dir == workspace / "auth"
        assert (workspace / "auth").is_dir()
        assert len(copied) == 1

    def test_seed_raises_on_copy_failure(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def bad_copytree(src, dst, **kwargs):
            raise OSError("disk full")

        with pytest.raises(RuntimeError, match="auth_seed_failed"):
            handler._seed_auth_dir(workspace, copy_fn=bad_copytree)

    def test_seed_raises_on_missing_src(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def missing_copytree(src, dst, **kwargs):
            raise FileNotFoundError(f"no such dir: {src}")

        with pytest.raises(RuntimeError, match="auth_seed_failed"):
            handler._seed_auth_dir(workspace, copy_fn=missing_copytree)

    def test_dispatch_issue_fails_fast_on_auth_seed_failure(self, handler, tmp_path: Path) -> None:
        _FakePopen.reset()
        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _seed_auth_fn=_failing_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert result["status"] == "error"
        assert result["reason"] == "auth_seed_failed"
        # No babysit was spawned.
        assert len(_FakePopen.instances) == 0
        # Workspace has exitcode=-1 (terminal state for supervision).
        workspace = Path(result["workspace"])
        assert (workspace / "exitcode").read_text() == "-1"

    def test_dispatch_issue_sets_claude_config_dir_env_for_babysit(self, handler, tmp_path: Path) -> None:
        _FakePopen.reset()
        handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert len(_FakePopen.instances) == 1
        env = _FakePopen.instances[0].kwargs.get("env")
        assert env is not None
        assert handler.CANONICAL_CLAUDE_DIR_ENV in env
        # Points to the per-dispatch auth dir, not the shared ~/.claude.
        auth_val = env[handler.CANONICAL_CLAUDE_DIR_ENV]
        assert "auth" in auth_val
        assert "dispatch-" in auth_val

    def test_exec_override_skips_auth_seeding(self, handler, tmp_path: Path) -> None:
        """exec_override mode (tests / smoke probes) must NOT copy credentials."""
        _FakePopen.reset()
        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            exec_override=["true"],
            supervision_mode="poll",
            _approval_cfg=_NO_GATE_CFG,
        )
        # Should succeed without any seed function.
        assert result["status"] == "launched"
        # env should be None (no CLAUDE_CONFIG_DIR override).
        env = _FakePopen.instances[0].kwargs.get("env")
        assert env is None


# ── Slot pool ─────────────────────────────────────────────────────────────


class TestSlotPool:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_try_acquire_slot_returns_index_on_success(self, handler, tmp_path: Path) -> None:
        slots_dir = handler._slots_dir(tmp_path)
        idx = handler._try_acquire_slot(slots_dir, "dispatch-test-0001")
        assert idx == 0  # first slot
        assert (slots_dir / "slot-0").exists()

    def test_try_acquire_slot_fills_in_order(self, handler, tmp_path: Path) -> None:
        slots_dir = handler._slots_dir(tmp_path)
        idx0 = handler._try_acquire_slot(slots_dir, "d-0")
        idx1 = handler._try_acquire_slot(slots_dir, "d-1")
        idx2 = handler._try_acquire_slot(slots_dir, "d-2")
        assert sorted([idx0, idx1, idx2]) == [0, 1, 2]

    def test_try_acquire_slot_returns_none_when_full(self, handler, tmp_path: Path) -> None:
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            assert handler._try_acquire_slot(slots_dir, f"d-{i}") is not None
        assert handler._try_acquire_slot(slots_dir, "d-overflow") is None

    def test_release_slot_removes_file(self, handler, tmp_path: Path) -> None:
        slots_dir = handler._slots_dir(tmp_path)
        idx = handler._try_acquire_slot(slots_dir, "d-x")
        assert idx is not None
        handler._release_slot(slots_dir, idx)
        assert not (slots_dir / f"slot-{idx}").exists()

    def test_release_slot_idempotent(self, handler, tmp_path: Path) -> None:
        slots_dir = handler._slots_dir(tmp_path)
        handler._release_slot(slots_dir, 0)  # slot that was never acquired — no error
        handler._release_slot(slots_dir, 0)  # again — no error

    def test_dispatch_issue_acquires_slot_and_records_in_response(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert result["status"] == "launched"
        assert result["slot"] in range(1, handler.POOL_SIZE + 1)

    def test_inline_mode_releases_slot_after_babysit_exits(self, handler, tmp_path: Path) -> None:
        _FakePopen.wait_returncode = 0
        handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="inline",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
        )
        # After inline dispatch, slot must be released.
        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        held = list(slots_dir.iterdir()) if slots_dir.exists() else []
        assert len(held) == 0, f"slot(s) not released: {[p.name for p in held]}"

    def test_poll_mode_launch_failed_releases_slot(self, handler, tmp_path: Path) -> None:
        def failing_popen(*args, **kwargs):
            raise OSError("no such file")

        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=failing_popen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert result["status"] == "launch_failed"
        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        held = list(slots_dir.iterdir()) if slots_dir.exists() else []
        assert len(held) == 0, f"slot not released on launch failure: {[p.name for p in held]}"

    def test_three_dispatches_each_get_a_slot(self, handler, tmp_path: Path) -> None:
        """Three concurrent dispatches each occupy a distinct slot (no interference)."""
        results = []
        for i in range(handler.POOL_SIZE):
            r = handler.dispatch_issue(
                issue_url=f"https://github.com/o/r/issues/{i}",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                workspace_root=tmp_path,
                popen=_FakePopen,
                supervision_mode="poll",
                _seed_auth_fn=_no_op_seed_auth,
                _approval_cfg=_NO_GATE_CFG,
            )
            results.append(r)

        assert all(r["status"] == "launched" for r in results)
        slot_nums = [r["slot"] for r in results]
        assert sorted(slot_nums) == list(range(1, handler.POOL_SIZE + 1))

    def test_fourth_dispatch_queues_then_gets_slot_when_first_releases(self, handler, tmp_path: Path) -> None:
        """The N+1-th dispatch blocks in the queue and proceeds once a slot frees."""
        # Fill all slots in poll mode (no babysit runs).
        for i in range(handler.POOL_SIZE):
            handler.dispatch_issue(
                issue_url=f"https://github.com/o/r/issues/{i}",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                workspace_root=tmp_path,
                popen=_FakePopen,
                supervision_mode="poll",
                _seed_auth_fn=_no_op_seed_auth,
                _approval_cfg=_NO_GATE_CFG,
            )

        # The 4th call should block until a slot is free. We free slot 0
        # from a background thread after a short delay.
        released = threading.Event()

        def _free_slot_after_delay():
            time.sleep(0.05)
            handler._release_slot(handler._slots_dir(tmp_path), 0)
            released.set()

        t = threading.Thread(target=_free_slot_after_delay, daemon=True)
        t.start()

        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/99",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _sleep_fn=lambda s: time.sleep(min(s, 0.01)),  # fast poll
            _approval_cfg=_NO_GATE_CFG,
        )

        t.join(timeout=5)
        assert released.is_set(), "background slot release timed out"
        assert result["status"] == "launched"
        assert result["slot"] in range(1, handler.POOL_SIZE + 1)


# ── FIFO queue ordering ───────────────────────────────────────────────────


class TestFifoQueue:
    def test_queue_ticket_sorts_by_creation_time(self, handler, tmp_path: Path) -> None:
        queue_dir = handler._queue_dir(tmp_path)
        t1 = handler._queue_ticket(queue_dir, "d-first")
        time.sleep(0.001)
        t2 = handler._queue_ticket(queue_dir, "d-second")
        tickets = sorted(p.name for p in queue_dir.iterdir() if p.is_file())
        assert tickets.index(t1.name) < tickets.index(t2.name)

    def test_queue_position_zero_for_front(self, handler, tmp_path: Path) -> None:
        queue_dir = handler._queue_dir(tmp_path)
        t = handler._queue_ticket(queue_dir, "d-solo")
        assert handler._queue_position(queue_dir, t.name) == 0

    def test_queue_position_increments_for_later_arrivals(self, handler, tmp_path: Path) -> None:
        queue_dir = handler._queue_dir(tmp_path)
        t1 = handler._queue_ticket(queue_dir, "d-a")
        time.sleep(0.001)
        t2 = handler._queue_ticket(queue_dir, "d-b")
        time.sleep(0.001)
        t3 = handler._queue_ticket(queue_dir, "d-c")
        assert handler._queue_position(queue_dir, t1.name) == 0
        assert handler._queue_position(queue_dir, t2.name) == 1
        assert handler._queue_position(queue_dir, t3.name) == 2

    def test_acquire_slot_removes_ticket_on_success(self, handler, tmp_path: Path) -> None:
        # Slot is free — ticket should be created then removed.
        slot_idx, _ = handler._acquire_slot(
            tmp_path,
            "d-x",
            _sleep_fn=lambda s: None,
            slack_token="skip",  # prevent real Slack calls
        )
        queue_dir = tmp_path / handler.POOL_QUEUE_DIR_NAME
        remaining = list(queue_dir.iterdir()) if queue_dir.exists() else []
        assert remaining == []

    def test_acquire_slot_removes_ticket_even_when_interrupted(self, handler, tmp_path: Path) -> None:
        """Ticket cleanup runs in finally — even if a KeyboardInterrupt fires."""
        # Fill the pool so _acquire_slot enters the slow path.
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        interrupt_fired = threading.Event()

        def _interrupt_after_delay():
            time.sleep(0.05)
            interrupt_fired.set()

        t = threading.Thread(target=_interrupt_after_delay, daemon=True)
        t.start()

        # Simulate a KeyboardInterrupt raised in the poll loop by making
        # the sleep function raise after the interrupt event is set.
        call_count = [0]

        def _raise_on_third(s):
            call_count[0] += 1
            if call_count[0] >= 3:
                # Free a slot first so _try_acquire_slot can succeed (avoids
                # an infinite loop after the interrupt check).
                handler._release_slot(slots_dir, 0)
            time.sleep(0.005)

        slot_idx, _ = handler._acquire_slot(
            tmp_path,
            "d-interrupted",
            _sleep_fn=_raise_on_third,
        )
        t.join(timeout=2)

        # Whatever happened, no stale ticket should remain.
        queue_dir = tmp_path / handler.POOL_QUEUE_DIR_NAME
        tickets = list(queue_dir.iterdir()) if queue_dir.exists() else []
        assert tickets == []


# ── dispatch_status verb ──────────────────────────────────────────────────


class TestDispatchStatus:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_empty_returns_empty_lists(self, handler, tmp_path: Path) -> None:
        status = handler.dispatch_status(workspace_root=tmp_path)
        assert status["running"] == []
        assert status["queued"] == []
        assert status["pool_size"] == handler.POOL_SIZE
        assert status["slots_free"] == handler.POOL_SIZE

    def test_running_reflects_active_slots(self, handler, tmp_path: Path) -> None:
        slots_dir = handler._slots_dir(tmp_path)
        handler._try_acquire_slot(slots_dir, "d-running-1")
        handler._try_acquire_slot(slots_dir, "d-running-2")

        status = handler.dispatch_status(workspace_root=tmp_path)
        assert set(status["running"]) == {"d-running-1", "d-running-2"}
        assert status["slots_free"] == handler.POOL_SIZE - 2

    def test_queued_reflects_waiting_tickets_in_fifo_order(self, handler, tmp_path: Path) -> None:
        queue_dir = handler._queue_dir(tmp_path)
        handler._queue_ticket(queue_dir, "d-first")
        time.sleep(0.001)
        handler._queue_ticket(queue_dir, "d-second")

        status = handler.dispatch_status(workspace_root=tmp_path)
        assert status["queued"] == ["d-first", "d-second"]

    def test_cli_dispatch_status_routes_correctly(self, handler, capsys) -> None:
        stub = {"running": [], "queued": [], "pool_size": 3, "slots_free": 3}
        with patch.object(handler, "dispatch_status", return_value=stub):
            rc = handler.run(["dispatch_status"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "running" in payload
        assert "queued" in payload

    def test_dispatch_status_now_known_verb(self, handler, capsys) -> None:
        """dispatch_status must appear in the known-verbs message."""
        rc = handler.run(["dispatch_frob"])
        assert rc == handler.EXIT_USAGE
        payload = json.loads(capsys.readouterr().out)
        assert "dispatch_status" in payload.get("message", "")


# ── Slack messages ────────────────────────────────────────────────────────


class TestSlackMessages:
    def test_post_slack_message_silent_when_no_channel(self, handler, monkeypatch) -> None:
        """Empty channel → silently return False regardless of token state."""
        monkeypatch.delenv(handler.WORKERS_BOT_TOKEN_ENV, raising=False)
        result = handler._post_slack_message("", "1.0", "hello")
        assert result is False

    def test_post_slack_message_raises_when_token_missing_and_posting_required(self, handler, monkeypatch) -> None:
        """WORKERS_BOT_TOKEN absent + non-empty channel → RuntimeError (fail-fast, see #241)."""
        monkeypatch.delenv(handler.WORKERS_BOT_TOKEN_ENV, raising=False)
        with pytest.raises(RuntimeError, match="WORKERS_BOT_TOKEN not set"):
            handler._post_slack_message("C1", "1.0", "hello")

    def test_post_slack_message_prefers_workers_token_over_injectable(self, handler, monkeypatch) -> None:
        """WORKERS_BOT_TOKEN env wins over the injectable token parameter."""
        workers_token = "xoxb-workers-env"
        monkeypatch.setenv(handler.WORKERS_BOT_TOKEN_ENV, workers_token)
        used_tokens: list = []

        def fake_urlopen(req, timeout=None):
            used_tokens.append(req.get_header("Authorization"))
            return None

        handler._post_slack_message("C1", "1.0", "hello", token="xoxb-injectable", _urlopen=fake_urlopen)
        assert len(used_tokens) == 1
        assert f"Bearer {workers_token}" in used_tokens[0]

    def test_post_slack_message_calls_urlopen(self, handler) -> None:
        opened: list = []

        def fake_urlopen(req, timeout=None):
            opened.append(req)
            return None

        result = handler._post_slack_message(
            "C1",
            "1.0",
            "hello",
            token="xoxb-fake",
            _urlopen=fake_urlopen,
        )
        assert result is True
        assert len(opened) == 1

    def test_post_slack_message_skips_when_urlopen_fails(self, handler) -> None:
        def boom(req, timeout=None):
            raise OSError("network down")

        result = handler._post_slack_message(
            "C1",
            "1.0",
            "hello",
            token="xoxb-fake",
            _urlopen=boom,
        )
        assert result is False

    def test_post_slack_message_raises_on_not_in_channel(self, handler) -> None:
        """not_in_channel Slack error propagates as RuntimeError (no silent swallow)."""
        import io
        import json as _json

        def not_in_channel_urlopen(req, timeout=None):
            body = _json.dumps({"ok": False, "error": "not_in_channel"}).encode()
            return io.BytesIO(body)

        with pytest.raises(RuntimeError, match="not_in_channel"):
            handler._post_slack_message("C1", "1.0", "hello", token="xoxb-fake", _urlopen=not_in_channel_urlopen)

    def test_post_slack_message_prefixes_with_dispatch_id_and_persona(self, handler) -> None:
        """Messages include [dispatch-<id> · <persona>] prefix when both are provided."""
        sent: list = []

        def capture(req, timeout=None):
            import json as _json

            body = _json.loads(req.data.decode())
            sent.append(body["text"])
            return None

        handler._post_slack_message(
            "C1",
            "1.0",
            "started",
            token="xoxb-fake",
            _urlopen=capture,
            dispatch_id="abc123",
            persona="sam",
        )
        assert sent == ["[dispatch-abc123 · sam] started"]

    def test_acquire_slot_posts_started_message_on_fast_path(self, handler, tmp_path: Path) -> None:
        posted: list[str] = []

        with patch.object(handler, "_post_slack_message", side_effect=lambda *a, **kw: posted.append(a[2]) or True):
            handler._acquire_slot(
                tmp_path,
                "d-fast",
                channel="C1",
                thread_ts="1.0",
                _sleep_fn=lambda s: None,
            )

        assert any("started" in m for m in posted)

    def test_acquire_slot_posts_queued_message_on_slow_path(self, handler, tmp_path: Path) -> None:
        # Fill all slots.
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        posted: list[str] = []
        freed = threading.Event()

        def _free_after_delay():
            time.sleep(0.03)
            handler._release_slot(slots_dir, 0)
            freed.set()

        t = threading.Thread(target=_free_after_delay, daemon=True)
        t.start()

        with patch.object(handler, "_post_slack_message", side_effect=lambda *a, **kw: posted.append(a[2]) or True):
            handler._acquire_slot(
                tmp_path,
                "d-slow",
                channel="C1",
                thread_ts="1.0",
                _sleep_fn=lambda s: time.sleep(min(s, 0.01)),
            )

        t.join(timeout=5)
        assert freed.is_set()
        # Should have posted both "queued" and "started".
        assert any("queued" in m for m in posted)
        assert any("started" in m for m in posted)


# ── Babysit slot release ──────────────────────────────────────────────────


class TestBabysitSlotRelease:
    def _load_babysit(self):
        if str(PACK_DIR) not in sys.path:
            sys.path.insert(0, str(PACK_DIR))
        spec = importlib.util.spec_from_file_location(
            "_test_pack_dispatch_d3_babysit",
            PACK_DIR / "babysit.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_babysit_releases_slot_on_normal_exit(self, tmp_path: Path) -> None:
        babysit = self._load_babysit()
        dispatch_id = "dispatch-test-babysit-release"

        # Create the slot file babysit should release.
        slots_dir = tmp_path / babysit.POOL_SLOTS_DIR_NAME
        slots_dir.mkdir()
        slot_path = slots_dir / "slot-0"
        slot_path.write_text(dispatch_id)

        # Patch _root() so babysit writes state files to tmp_path.
        import unittest.mock as mock

        with mock.patch.object(babysit, "_root", return_value=tmp_path):
            # Run babysit with a fast-exiting command.
            rc = babysit.run(
                dispatch_id=dispatch_id,
                cmd=["true"],
                cwd=str(tmp_path),
                slot_idx=0,
            )

        assert rc == 0
        # Slot must be released.
        assert not slot_path.exists()

    def test_babysit_slot_idx_minus_one_is_noop(self, tmp_path: Path) -> None:
        babysit = self._load_babysit()
        # _release_slot with -1 must not raise or touch anything.
        babysit._release_slot(-1)  # no error

    def test_babysit_argv_includes_slot_idx(self, handler, tmp_path: Path) -> None:
        _FakePopen.reset()
        handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/1",
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
        assert "--slot-idx" in argv
        slot_idx_pos = argv.index("--slot-idx")
        slot_idx_val = int(argv[slot_idx_pos + 1])
        assert 0 <= slot_idx_val < handler.POOL_SIZE


# ── Issue #220: error detail logging + Slack ─────────────────────────────────


class TestErrorDetailLogging:
    """Regression tests: auth_seed_failed emits logger.error and Slack detail."""

    def test_auth_seed_failed_emits_logger_error(self, handler, tmp_path: Path, caplog) -> None:
        """logger.error must include dispatch_id, reason, and detail."""
        import logging

        _FakePopen.reset()
        with caplog.at_level(logging.ERROR, logger="dispatch.handler"):
            handler.dispatch_issue(
                issue_url="https://github.com/o/r/issues/1",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                workspace_root=tmp_path,
                popen=_FakePopen,
                _seed_auth_fn=_failing_seed_auth,
                _approval_cfg=_NO_GATE_CFG,
            )

        assert any("auth_seed_failed" in r.message for r in caplog.records), (
            f"Expected 'auth_seed_failed' in log records; got: {caplog.records}"
        )
        assert any("disk full" in r.message for r in caplog.records), (
            "Expected failure detail 'disk full' in log records"
        )

    def test_auth_seed_failed_posts_to_slack(self, handler, tmp_path: Path) -> None:
        """Slack post must include redacted detail when token is provided."""
        _FakePopen.reset()
        posted: list[str] = []

        def fake_slack(channel, thread_ts, text, *, token=None, **kwargs):
            posted.append(text)
            return True

        with patch.object(handler, "_post_slack_message", side_effect=fake_slack):
            handler.dispatch_issue(
                issue_url="https://github.com/o/r/issues/1",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                workspace_root=tmp_path,
                popen=_FakePopen,
                _seed_auth_fn=_failing_seed_auth,
                _slack_token="xoxb-test",
                _approval_cfg=_NO_GATE_CFG,
            )

        assert posted, "Expected at least one Slack post"
        combined = "\n".join(posted)
        assert "auth_seed_failed" in combined
        assert "disk full" in combined

    def test_auth_seed_failed_slack_redacts_pat(self, handler, tmp_path: Path) -> None:
        """GitHub PATs must not appear in the Slack message."""

        def seed_with_pat(workspace: Path) -> Path:
            raise RuntimeError("auth_seed_failed: token ghp_ABC123abc123abc123abc123abc123 invalid")

        posted: list[str] = []

        def fake_slack(channel, thread_ts, text, *, token=None, **kwargs):
            posted.append(text)
            return True

        with patch.object(handler, "_post_slack_message", side_effect=fake_slack):
            handler.dispatch_issue(
                issue_url="https://github.com/o/r/issues/1",
                channel="C1",
                thread_ts="1.0",
                agent="sam",
                workspace_root=tmp_path,
                popen=_FakePopen,
                _seed_auth_fn=seed_with_pat,
                _slack_token="xoxb-test",
                _approval_cfg=_NO_GATE_CFG,
            )

        combined = "\n".join(posted)
        assert "ghp_" not in combined, "PAT must be redacted from Slack message"
        assert "[REDACTED]" in combined
