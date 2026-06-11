"""Unit tests for issue #318: FIFO queue error handling and advancement logging.

Covers:
- bare except Exception replaced with targeted (FileNotFoundError, OSError).
- logger.warning on each _queue_position failure with structured fields.
- After _QUEUE_CORRUPT_THRESHOLD consecutive failures, escalates to
  logger.error, posts a Slack notice, and raises QueueCorruptError.
- dispatch_issue returns reason=queue_corrupt when QueueCorruptError propagates.
- logger.info emitted exactly once per queue position change (advancement).
- Consecutive failure counter resets to zero after a successful _queue_position call.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
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
        "_test_pack_dispatch_issue318_handler",
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


def _no_op_clone(workspace: Path, issue_url: str) -> Path:
    return workspace / "repo"


_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


# ── queue_position failure → warning ────────────────────────────────────────


class TestQueuePositionWarning:
    def test_oserror_logs_warning(self, handler, tmp_path: Path, caplog) -> None:
        """A single OSError logs a structured warning and does not crash the loop."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        call_count = [0]

        def fake_pos(queue_dir, ticket_name):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("queue dir gone")
            handler._release_slot(slots_dir, 0)
            return 0

        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=fake_pos):
                handler._acquire_slot(
                    tmp_path,
                    "d-warn-test",
                    _sleep_fn=lambda s: None,
                )

        warnings = [r for r in caplog.records if "queue_position_failed" in r.message]
        assert len(warnings) >= 1

    def test_warning_contains_structured_fields(self, handler, tmp_path: Path, caplog) -> None:
        """Warning message includes dispatch_id, ticket name, and error repr."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        call_count = [0]

        def fake_pos(queue_dir, ticket_name):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("disk I/O error")
            handler._release_slot(slots_dir, 0)
            return 0

        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=fake_pos):
                handler._acquire_slot(
                    tmp_path,
                    "dispatch-structured-test",
                    _sleep_fn=lambda s: None,
                )

        warn_msgs = [r.message for r in caplog.records if "queue_position_failed" in r.message]
        assert warn_msgs, "Expected at least one queue_position_failed warning"
        msg = warn_msgs[0]
        assert "dispatch-structured-test" in msg
        assert "disk I/O error" in msg

    def test_single_failure_does_not_raise(self, handler, tmp_path: Path) -> None:
        """A single OSError is tolerated — dispatch proceeds normally."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        call_count = [0]

        def fake_pos(queue_dir, ticket_name):
            call_count[0] += 1
            if call_count[0] <= 5:
                raise OSError("transient")
            handler._release_slot(slots_dir, 0)
            return 0

        with patch.object(handler, "_queue_position", side_effect=fake_pos):
            slot_idx, slot_num = handler._acquire_slot(
                tmp_path,
                "d-tolerate-test",
                _sleep_fn=lambda s: None,
            )
        assert slot_idx is not None


# ── threshold → QueueCorruptError ────────────────────────────────────────────


class TestQueueCorruptThreshold:
    def test_threshold_value_is_30(self, handler) -> None:
        """_QUEUE_CORRUPT_THRESHOLD must be 30 per spec."""
        assert handler._QUEUE_CORRUPT_THRESHOLD == 30

    def test_threshold_raises_queue_corrupt_error(self, handler, tmp_path: Path, caplog) -> None:
        """After >= 30 consecutive OSErrors, QueueCorruptError is raised."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        def always_fails(queue_dir, ticket_name):
            raise OSError("persistent failure")

        with caplog.at_level(logging.ERROR, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=always_fails):
                with pytest.raises(handler.QueueCorruptError, match="queue_corrupt"):
                    handler._acquire_slot(
                        tmp_path,
                        "d-corrupt-test",
                        _sleep_fn=lambda s: None,
                    )

        error_recs = [r for r in caplog.records if r.levelno == logging.ERROR and "queue_corrupt" in r.message]
        assert error_recs, "Expected logger.error with queue_corrupt after threshold"

    def test_threshold_posts_slack_notice(self, handler, tmp_path: Path) -> None:
        """A Slack notice is posted before raising QueueCorruptError."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        posted: list[str] = []

        def fake_slack(channel, thread_ts, text, **kwargs):
            posted.append(text)
            return True

        def always_fails(queue_dir, ticket_name):
            raise OSError("corrupt")

        with patch.object(handler, "_queue_position", side_effect=always_fails):
            with patch.object(handler, "_post_slack_message", side_effect=fake_slack):
                with pytest.raises(handler.QueueCorruptError):
                    handler._acquire_slot(
                        tmp_path,
                        "d-slack-test",
                        channel="C1",
                        thread_ts="1.0",
                        _sleep_fn=lambda s: None,
                    )

        assert any("queue_corrupt" in m for m in posted), f"No queue_corrupt Slack notice; got: {posted}"

    def test_ticket_removed_after_queue_corrupt(self, handler, tmp_path: Path) -> None:
        """The finally block removes the queue ticket even on QueueCorruptError."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        def always_fails(queue_dir, ticket_name):
            raise OSError("corrupt")

        with patch.object(handler, "_queue_position", side_effect=always_fails):
            with pytest.raises(handler.QueueCorruptError):
                handler._acquire_slot(
                    tmp_path,
                    "d-cleanup-test",
                    _sleep_fn=lambda s: None,
                )

        queue_dir = tmp_path / handler.POOL_QUEUE_DIR_NAME
        tickets = list(queue_dir.iterdir()) if queue_dir.exists() else []
        assert tickets == [], f"Stale ticket(s) after QueueCorruptError: {[t.name for t in tickets]}"

    def test_consecutive_failures_reset_on_success(self, handler, tmp_path: Path, caplog) -> None:
        """Counter resets to 0 when _queue_position succeeds; 29 failures then success should NOT raise."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        call_count = [0]

        def intermittent(queue_dir, ticket_name):
            call_count[0] += 1
            if call_count[0] <= 29:
                raise OSError("transient")
            handler._release_slot(slots_dir, 0)
            return 0

        with caplog.at_level(logging.ERROR, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=intermittent):
                handler._acquire_slot(
                    tmp_path,
                    "d-reset-test",
                    _sleep_fn=lambda s: None,
                )

        error_recs = [r for r in caplog.records if r.levelno == logging.ERROR and "queue_corrupt" in r.message]
        assert not error_recs, "Should not escalate — 29 failures < threshold 30"

    def test_dispatch_issue_returns_queue_corrupt_reason(self, handler, tmp_path: Path) -> None:
        """dispatch_issue catches QueueCorruptError and returns reason=queue_corrupt."""

        def failing_acquire(root, dispatch_id, **kwargs):
            raise handler.QueueCorruptError("queue_corrupt: queue_position failed 30 consecutive times")

        def _must_not_call(*a, **kw):
            raise AssertionError("popen must not be called")

        with patch.object(handler, "_acquire_slot", side_effect=failing_acquire):
            result = handler.dispatch_issue(
                issue_url="https://github.com/o/r/issues/1",
                channel="",
                thread_ts="1.0",
                agent="sam",
                workspace_root=tmp_path,
                popen=_must_not_call,
                supervision_mode="poll",
                _seed_auth_fn=_no_op_seed_auth,
                _clone_repo_fn=_no_op_clone,
                _approval_cfg=_NO_GATE_CFG,
            )

        assert result["status"] == "error"
        assert result["reason"] == "queue_corrupt"
        workspace = Path(result["workspace"])
        assert (workspace / "exitcode").exists(), "exitcode file must be written on queue_corrupt"


# ── Queue advancement logging ────────────────────────────────────────────────


class TestQueueAdvancementLogging:
    def test_one_log_per_position_change(self, handler, tmp_path: Path, caplog) -> None:
        """queue_advanced is logged exactly once per position change."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        # Sequence: initial my_pos=0, then positions 2, 1, 0.
        # Changes: 0→2, 2→1, 1→0 → expect 3 log lines.
        positions = iter([2, 1, 0])

        def advancing_pos(queue_dir, ticket_name):
            try:
                pos = next(positions)
            except StopIteration:
                pos = 0
            if pos == 0:
                handler._release_slot(slots_dir, 0)
            return pos

        with caplog.at_level(logging.INFO, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=advancing_pos):
                handler._acquire_slot(
                    tmp_path,
                    "d-advance-test",
                    _sleep_fn=lambda s: None,
                )

        adv_logs = [r for r in caplog.records if "queue_advanced" in r.message]
        assert len(adv_logs) == 3, f"Expected 3 advancement logs (0→2, 2→1, 1→0); got {len(adv_logs)}"

    def test_no_log_when_position_unchanged(self, handler, tmp_path: Path, caplog) -> None:
        """No queue_advanced log is emitted when position stays the same across ticks."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        # initial my_pos=0; ticks: 2 (log 0→2), 2, 2, 0 (log 2→0) = 2 total logs
        call_count = [0]

        def stable_then_zero(queue_dir, ticket_name):
            call_count[0] += 1
            if call_count[0] == 1:
                return 2
            if call_count[0] <= 3:
                return 2  # unchanged — no log
            handler._release_slot(slots_dir, 0)
            return 0

        with caplog.at_level(logging.INFO, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=stable_then_zero):
                handler._acquire_slot(
                    tmp_path,
                    "d-stable-test",
                    _sleep_fn=lambda s: None,
                )

        adv_logs = [r for r in caplog.records if "queue_advanced" in r.message]
        assert len(adv_logs) == 2, (
            f"Expected 2 logs (0→2 and 2→0), not one per tick; got {len(adv_logs)}: {[r.message for r in adv_logs]}"
        )

    def test_advancement_log_contains_dispatch_id_and_pos(self, handler, tmp_path: Path, caplog) -> None:
        """queue_advanced log includes the dispatch_id and new position."""
        slots_dir = handler._slots_dir(tmp_path)
        for i in range(handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"d-pre-{i}")

        call_count = [0]

        def pos_fn(queue_dir, ticket_name):
            call_count[0] += 1
            if call_count[0] == 1:
                return 1
            handler._release_slot(slots_dir, 0)
            return 0

        with caplog.at_level(logging.INFO, logger="dispatch.handler"):
            with patch.object(handler, "_queue_position", side_effect=pos_fn):
                handler._acquire_slot(
                    tmp_path,
                    "dispatch-logfields-test",
                    _sleep_fn=lambda s: None,
                )

        adv_logs = [r for r in caplog.records if "queue_advanced" in r.message]
        assert adv_logs, "Expected at least one queue_advanced log"
        # dispatch_id should appear in at least one advancement log
        assert any("dispatch-logfields-test" in r.message for r in adv_logs)
