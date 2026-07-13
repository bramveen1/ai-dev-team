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

from router.auto_dispatch.inflight import _run_periodic_orphan_sweep

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
