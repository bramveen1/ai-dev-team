"""Thin dedicated tests for router.auto_dispatch.inflight (#719).

inflight.py had no dedicated test file. This covers the
``_run_periodic_orphan_sweep`` entry point: it must delegate to
``janitor.sweep_orphans`` with the given workspace root, and it must never
raise (best-effort per its docstring) even when the sweep itself blows up.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from router.auto_dispatch.inflight import _find_terminal_dispatch_for_issue, _run_periodic_orphan_sweep
from router.dispatch import state as dstate

pytestmark = pytest.mark.unit

_PACK_DIR = Path(__file__).resolve().parents[3] / "packs" / "dispatch"
if _PACK_DIR.is_dir() and str(_PACK_DIR) not in sys.path:
    sys.path.insert(0, str(_PACK_DIR))


class TestRunPeriodicOrphanSweep:
    def test_delegates_to_janitor_sweep_orphans_with_root(self, tmp_path):
        with patch("janitor.sweep_orphans", return_value={"aged_out": 0, "errors": 0}) as sweep:
            _run_periodic_orphan_sweep(workspace_root=str(tmp_path))
        sweep.assert_called_once_with(str(tmp_path))

    def test_defaults_root_to_var_lib_dispatch(self):
        with patch("janitor.sweep_orphans", return_value={"aged_out": 0, "errors": 0}) as sweep:
            _run_periodic_orphan_sweep()
        sweep.assert_called_once_with("/var/lib/dispatch")

    def test_sweep_exception_is_swallowed_not_raised(self, tmp_path):
        with patch("janitor.sweep_orphans", side_effect=RuntimeError("boom")):
            _run_periodic_orphan_sweep(workspace_root=str(tmp_path))  # must not raise

    def test_aged_out_entries_are_logged(self, tmp_path, caplog):
        with (
            patch("janitor.sweep_orphans", return_value={"aged_out": 2, "errors": 0}),
            caplog.at_level("INFO", logger="router.auto_dispatch.inflight"),
        ):
            _run_periodic_orphan_sweep(workspace_root=str(tmp_path))
        assert any("aged_out=2" in r.message for r in caplog.records)


class TestFindTerminalDispatchForIssue:
    """#867: the mirror image of `_get_in_flight_issue_nums` — finds a
    dispatch that has already written a terminal exitcode, so the age-out
    sweep can tell a hard-failed worker apart from one still running."""

    def _make_dispatch(self, root, dispatch_id, *, issue_num, exit_code=None):
        dstate.write_field(dispatch_id, dstate.FIELD_ISSUE_URL, f"https://github.com/o/r/issues/{issue_num}", root=root)
        if exit_code is not None:
            dstate.write_field(dispatch_id, dstate.FIELD_EXITCODE, str(exit_code), root=root)

    def test_no_dispatches_returns_none(self, tmp_path):
        assert _find_terminal_dispatch_for_issue(101, dispatch_root_override=str(tmp_path)) is None

    def test_alive_dispatch_no_exitcode_returns_none(self, tmp_path):
        self._make_dispatch(str(tmp_path), "dispatch-1", issue_num=101)
        assert _find_terminal_dispatch_for_issue(101, dispatch_root_override=str(tmp_path)) is None

    def test_terminal_dispatch_for_other_issue_is_ignored(self, tmp_path):
        self._make_dispatch(str(tmp_path), "dispatch-1", issue_num=999, exit_code=1)
        assert _find_terminal_dispatch_for_issue(101, dispatch_root_override=str(tmp_path)) is None

    def test_terminal_dispatch_returns_exit_code_and_dispatch_id(self, tmp_path):
        self._make_dispatch(str(tmp_path), "dispatch-1", issue_num=101, exit_code=1)
        result = _find_terminal_dispatch_for_issue(101, dispatch_root_override=str(tmp_path))
        assert result == {"dispatch_id": "dispatch-1", "exit_code": 1, "result_text": ""}

    def test_non_integer_exitcode_defaults_to_negative_one(self, tmp_path):
        root = str(tmp_path)
        dstate.write_field("dispatch-1", dstate.FIELD_ISSUE_URL, "https://github.com/o/r/issues/101", root=root)
        dstate.write_field("dispatch-1", dstate.FIELD_EXITCODE, "not-a-number", root=root)
        result = _find_terminal_dispatch_for_issue(101, dispatch_root_override=root)
        assert result["exit_code"] == -1

    def test_most_recent_terminal_dispatch_wins_on_multiple_matches(self, tmp_path):
        self._make_dispatch(str(tmp_path), "dispatch-20260101T000000-aaa", issue_num=101, exit_code=1)
        self._make_dispatch(str(tmp_path), "dispatch-20260201T000000-bbb", issue_num=101, exit_code=0)
        result = _find_terminal_dispatch_for_issue(101, dispatch_root_override=str(tmp_path))
        assert result["dispatch_id"] == "dispatch-20260201T000000-bbb"
        assert result["exit_code"] == 0

    def test_result_text_pulled_from_transcript_tail(self, tmp_path):
        self._make_dispatch(str(tmp_path), "dispatch-1", issue_num=101, exit_code=1)
        d = dstate.dispatch_dir("dispatch-1", root=str(tmp_path))
        (d / dstate.FIELD_TRANSCRIPT).write_text('{"type": "result", "result": "Not logged in"}\n')
        result = _find_terminal_dispatch_for_issue(101, dispatch_root_override=str(tmp_path))
        assert result["result_text"] == "Not logged in"
