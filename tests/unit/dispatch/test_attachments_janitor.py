"""Unit tests for packs.dispatch.attachments_janitor — thread dir sweep (#327)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_PACK_DIR = str(Path(__file__).parents[3] / "packs" / "dispatch")
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

import attachments_janitor as _aj  # noqa: E402

pytestmark = pytest.mark.unit

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_TS = _NOW.timestamp()


def _make_thread_dir(root: Path, thread_id: str, *, age_seconds: int, size_bytes: int = 0) -> Path:
    """Create a fake thread dir with backdated mtime and optional file content."""
    td = root / thread_id
    td.mkdir()
    if size_bytes:
        (td / "file.bin").write_bytes(b"x" * size_bytes)
    old_ts = _NOW_TS - age_seconds
    os.utime(td, (old_ts, old_ts))
    return td


def _collect_log(capsys) -> list[dict]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.strip()]


# ── Sweep: TTL boundary ───────────────────────────────────────────────────────


class TestSweepTTLBoundary:
    def test_old_thread_dir_is_removed(self, tmp_path, capsys):
        """Thread dir with mtime older than TTL → removed."""
        _make_thread_dir(tmp_path, "1234567890.000100", age_seconds=8 * 86400)

        result = _aj.sweep(str(tmp_path), now=_NOW, ttl_days=7)

        assert result["removed"] == 1
        assert result["kept"] == 0
        assert result["errors"] == 0
        assert not (tmp_path / "1234567890.000100").exists()
        logs = _collect_log(capsys)
        removed_events = [e for e in logs if e["event"] == "attachments_thread_removed"]
        assert len(removed_events) == 1
        assert removed_events[0]["thread_id"] == "1234567890.000100"

    def test_fresh_thread_dir_is_kept(self, tmp_path, capsys):
        """Thread dir with mtime within TTL → not removed."""
        _make_thread_dir(tmp_path, "1234567890.000200", age_seconds=2 * 86400)

        result = _aj.sweep(str(tmp_path), now=_NOW, ttl_days=7)

        assert result["kept"] == 1
        assert result["removed"] == 0
        assert (tmp_path / "1234567890.000200").exists()

    def test_exactly_at_ttl_boundary_is_removed(self, tmp_path):
        """age == ttl_seconds exactly triggers removal (>= comparison)."""
        _make_thread_dir(tmp_path, "boundary-thread", age_seconds=7 * 86400)

        result = _aj.sweep(str(tmp_path), now=_NOW, ttl_days=7)

        assert result["removed"] == 1
        assert not (tmp_path / "boundary-thread").exists()

    def test_bytes_freed_accounts_for_file_content(self, tmp_path):
        """bytes_freed in the summary equals the actual content removed."""
        _make_thread_dir(tmp_path, "thread-with-files", age_seconds=8 * 86400, size_bytes=1024)

        result = _aj.sweep(str(tmp_path), now=_NOW, ttl_days=7)

        assert result["bytes_freed"] == 1024

    def test_mixed_old_and_fresh_dirs(self, tmp_path, capsys):
        """Old dirs removed, fresh dirs kept; summary is accurate."""
        _make_thread_dir(tmp_path, "old-thread", age_seconds=8 * 86400)
        _make_thread_dir(tmp_path, "fresh-thread", age_seconds=1 * 86400)

        result = _aj.sweep(str(tmp_path), now=_NOW, ttl_days=7)

        assert result["removed"] == 1
        assert result["kept"] == 1
        assert not (tmp_path / "old-thread").exists()
        assert (tmp_path / "fresh-thread").exists()


# ── Sweep: edge cases ─────────────────────────────────────────────────────────


class TestSweepEdgeCases:
    def test_empty_root_returns_zero_summary(self, tmp_path, capsys):
        """Empty attachments root → all-zero summary without raising."""
        result = _aj.sweep(str(tmp_path), now=_NOW)

        assert result == {"removed": 0, "kept": 0, "errors": 0, "bytes_freed": 0}
        logs = _collect_log(capsys)
        summary = [e for e in logs if e["event"] == "attachments_sweep_summary"]
        assert len(summary) == 1
        assert summary[0]["removed"] == 0

    def test_missing_root_does_not_raise(self, tmp_path, capsys):
        """Missing attachments root → returns zeros, logs failure."""
        absent = str(tmp_path / "nonexistent")

        result = _aj.sweep(absent, now=_NOW)

        assert result == {"removed": 0, "kept": 0, "errors": 0, "bytes_freed": 0}
        logs = _collect_log(capsys)
        failed = [e for e in logs if e["event"] == "attachments_sweep_failed"]
        assert len(failed) == 1
        assert "root missing" in failed[0]["reason"]

    def test_non_directory_entries_are_skipped(self, tmp_path):
        """Regular files at the root level are not processed."""
        (tmp_path / "some-file.txt").write_text("data")

        result = _aj.sweep(str(tmp_path), now=_NOW)

        assert result["removed"] == 0
        assert result["kept"] == 0

    def test_permission_error_is_isolated(self, tmp_path, capsys):
        """PermissionError on one dir is counted in errors; others still swept."""
        _make_thread_dir(tmp_path, "thread-ok", age_seconds=8 * 86400)
        _make_thread_dir(tmp_path, "thread-bad", age_seconds=8 * 86400)

        original_rmtree = __import__("shutil").rmtree

        def _patched_rmtree(path, **kwargs):
            if "thread-bad" in str(path):
                raise PermissionError("permission denied")
            original_rmtree(path, **kwargs)

        with patch("attachments_janitor.shutil.rmtree", side_effect=_patched_rmtree):
            result = _aj.sweep(str(tmp_path), now=_NOW, ttl_days=7)

        assert result["removed"] == 1
        assert result["errors"] == 1
        logs = _collect_log(capsys)
        error_events = [e for e in logs if e["event"] == "attachments_sweep_error"]
        assert len(error_events) == 1
        assert error_events[0]["thread_id"] == "thread-bad"


# ── Disk-pressure guard ───────────────────────────────────────────────────────


class TestDiskPressure:
    def test_over_threshold_returns_true_and_logs(self, tmp_path, capsys):
        """Returns True and emits a log when usage >= threshold."""
        from collections import namedtuple

        DiskUsage = namedtuple("usage", ["total", "used", "free"])
        fake_usage = DiskUsage(total=100, used=85, free=15)
        with patch("attachments_janitor.shutil.disk_usage", return_value=fake_usage):
            result = _aj.check_disk_pressure(str(tmp_path), threshold_pct=80)

        assert result is True
        logs = _collect_log(capsys)
        pressure_events = [e for e in logs if e["event"] == "attachments_disk_pressure"]
        assert len(pressure_events) == 1
        ev = pressure_events[0]
        assert ev["threshold_pct"] == 80
        assert ev["used_pct"] == 85.0

    def test_at_threshold_returns_true(self, tmp_path, capsys):
        """Exactly at threshold (>= comparison) → True."""
        from collections import namedtuple

        DiskUsage = namedtuple("usage", ["total", "used", "free"])
        fake_usage = DiskUsage(total=100, used=80, free=20)
        with patch("attachments_janitor.shutil.disk_usage", return_value=fake_usage):
            result = _aj.check_disk_pressure(str(tmp_path), threshold_pct=80)

        assert result is True

    def test_under_threshold_returns_false(self, tmp_path, capsys):
        """Returns False when usage is below threshold."""
        from collections import namedtuple

        DiskUsage = namedtuple("usage", ["total", "used", "free"])
        fake_usage = DiskUsage(total=100, used=50, free=50)
        with patch("attachments_janitor.shutil.disk_usage", return_value=fake_usage):
            result = _aj.check_disk_pressure(str(tmp_path), threshold_pct=80)

        assert result is False
        logs = _collect_log(capsys)
        assert not any(e.get("event") == "attachments_disk_pressure" for e in logs)

    def test_missing_root_returns_false_without_raising(self, tmp_path):
        """Non-existent attachments root → False, no exception."""
        absent = str(tmp_path / "nonexistent")
        result = _aj.check_disk_pressure(absent)
        assert result is False

    def test_oserror_returns_false_without_raising(self, tmp_path):
        """OSError from disk_usage → False, no exception."""
        with patch("attachments_janitor.shutil.disk_usage", side_effect=OSError("no stat")):
            result = _aj.check_disk_pressure(str(tmp_path))
        assert result is False
