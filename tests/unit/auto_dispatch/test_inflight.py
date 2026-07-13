"""Thin dedicated tests for router.auto_dispatch.inflight (#719).

inflight.py had no dedicated test file — ``_run_periodic_orphan_sweep`` was
only exercised incidentally via loop-level patches in test_auto_dispatch.py.
This covers its direct, un-mocked behaviour: aging out stale ``_orphans/``
entries and staying a safe no-op otherwise.
"""

from __future__ import annotations

import os
import time

import pytest

from router.auto_dispatch.inflight import _run_periodic_orphan_sweep

pytestmark = pytest.mark.unit


class TestRunPeriodicOrphanSweep:
    def test_old_orphan_entry_is_aged_out(self, tmp_path):
        orphans_dir = tmp_path / "_orphans"
        orphans_dir.mkdir()
        old_entry = orphans_dir / "20240101T000000Z-dispatch-old"
        old_entry.mkdir()
        old_ts = time.time() - 8 * 86400  # older than the 7-day ORPHAN_TTL_DAYS
        os.utime(old_entry, (old_ts, old_ts))

        _run_periodic_orphan_sweep(workspace_root=str(tmp_path))

        assert not old_entry.exists()

    def test_missing_orphans_dir_is_a_safe_noop(self, tmp_path):
        _run_periodic_orphan_sweep(workspace_root=str(tmp_path))  # must not raise
