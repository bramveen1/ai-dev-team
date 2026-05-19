"""Unit tests for packs.dispatch.janitor — orphan workspace sweep."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# janitor.py lives alongside handler.py in packs/dispatch/; add that dir
# to sys.path so we can import it directly (same approach as
# test_pack_dispatch.py and test_pack_dispatch_quota.py).
_PACK_DIR = str(Path(__file__).parents[3] / "packs" / "dispatch")
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

import janitor as _janitor

pytestmark = pytest.mark.unit

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_TS = _NOW.timestamp()


def _make_workspace(root: Path, name: str, *, age_seconds: int, has_exitcode: bool = False) -> Path:
    """Create a fake dispatch workspace with backdated mtime."""
    ws = root / name
    ws.mkdir()
    (ws / "started_at").write_text("2024-06-01T00:00:00+00:00")
    if has_exitcode:
        (ws / "exitcode").write_text("0")
    # Backdate both the directory and a child file so stat().st_mtime reflects age.
    old_ts = _NOW_TS - age_seconds
    os.utime(ws, (old_ts, old_ts))
    return ws


def _collect_log(capsys) -> list[dict]:
    """Parse all JSON lines from stderr captured during a sweep call."""
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.strip()]


# ── Positive paths ────────────────────────────────────────────────────────


class TestOrphanMoved:
    def test_no_exitcode_old_mtime_is_moved(self, tmp_path, capsys):
        """Workspace with no exitcode and mtime ≥ grace → moved to _orphans/."""
        _make_workspace(tmp_path, "dispatch-old", age_seconds=600)

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 1
        assert result["skipped_live"] == 0
        assert result["errors"] == 0
        # Original dir is gone.
        assert not (tmp_path / "dispatch-old").exists()
        # Ended up in _orphans/.
        orphans = list((tmp_path / "_orphans").iterdir())
        assert len(orphans) == 1
        assert orphans[0].name.endswith("-dispatch-old")
        # Log line emitted.
        logs = _collect_log(capsys)
        moved_events = [e for e in logs if e["event"] == "orphan_moved"]
        assert len(moved_events) == 1
        assert moved_events[0]["dispatch_id"] == "dispatch-old"
        assert moved_events[0]["had_exitcode"] is False

    def test_has_exitcode_is_moved(self, tmp_path, capsys):
        """Workspace with exitcode present (terminal-but-uncleaned) → moved."""
        _make_workspace(tmp_path, "dispatch-done", age_seconds=30, has_exitcode=True)

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 1
        assert not (tmp_path / "dispatch-done").exists()
        logs = _collect_log(capsys)
        moved_events = [e for e in logs if e["event"] == "orphan_moved"]
        assert moved_events[0]["had_exitcode"] is True

    def test_moved_dir_preserves_logs(self, tmp_path):
        """Children of the workspace survive the rename into _orphans/."""
        ws = _make_workspace(tmp_path, "dispatch-logs", age_seconds=600)
        transcript = ws / "transcript.jsonl"
        transcript.write_text('{"type":"text"}\n')
        # Backdate dir again after writing the child so mtime reflects age.
        old_ts = _NOW_TS - 600
        os.utime(ws, (old_ts, old_ts))

        _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        orphans = list((tmp_path / "_orphans").iterdir())
        assert len(orphans) == 1
        assert (orphans[0] / "transcript.jsonl").exists()


class TestOrphanAgedOut:
    def test_old_orphan_entry_is_deleted(self, tmp_path, capsys):
        """_orphans/ entry with mtime > TTL → deleted."""
        orphans_dir = tmp_path / "_orphans"
        orphans_dir.mkdir()
        entry = orphans_dir / "20240101T000000Z-dispatch-stale"
        entry.mkdir()
        # Backdate 8 days.
        old_ts = _NOW_TS - 8 * 86400
        os.utime(entry, (old_ts, old_ts))

        result = _janitor.sweep(str(tmp_path), now=_NOW, orphan_ttl_days=7)

        assert result["aged_out"] == 1
        assert not entry.exists()
        logs = _collect_log(capsys)
        aged_events = [e for e in logs if e["event"] == "orphan_aged_out"]
        assert len(aged_events) == 1
        assert aged_events[0]["dispatch_id"] == "dispatch-stale"

    def test_recent_orphan_entry_is_kept(self, tmp_path):
        """_orphans/ entry within TTL → not deleted."""
        orphans_dir = tmp_path / "_orphans"
        orphans_dir.mkdir()
        entry = orphans_dir / "20240601T000000Z-dispatch-fresh"
        entry.mkdir()
        # 2 days old — well within 7-day TTL.
        fresh_ts = _NOW_TS - 2 * 86400
        os.utime(entry, (fresh_ts, fresh_ts))

        result = _janitor.sweep(str(tmp_path), now=_NOW, orphan_ttl_days=7)

        assert result["aged_out"] == 0
        assert entry.exists()


class TestEmptyRoot:
    def test_empty_dispatch_root_returns_zero_summary(self, tmp_path, capsys):
        """Empty /var/lib/dispatch/ → returns all-zero summary without raising."""
        result = _janitor.sweep(str(tmp_path), now=_NOW)

        assert result == {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0}
        logs = _collect_log(capsys)
        summary = [e for e in logs if e["event"] == "janitor_summary"]
        assert len(summary) == 1
        assert summary[0] == {"event": "janitor_summary", "moved": 0, "aged_out": 0,
                               "skipped_live": 0, "errors": 0}


# ── Negative paths ────────────────────────────────────────────────────────


class TestGraceWindow:
    def test_fresh_workspace_is_not_touched(self, tmp_path):
        """Workspace mtime within grace_seconds → skipped_live, not moved."""
        _make_workspace(tmp_path, "dispatch-fresh", age_seconds=10)

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["skipped_live"] == 1
        assert result["moved"] == 0
        assert (tmp_path / "dispatch-fresh").exists()

    def test_exactly_at_grace_boundary_is_moved(self, tmp_path):
        """age == grace_seconds exactly is treated as orphan (>= comparison)."""
        _make_workspace(tmp_path, "dispatch-boundary", age_seconds=60)

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 1
        assert not (tmp_path / "dispatch-boundary").exists()


class TestReservedNames:
    @pytest.mark.parametrize("reserved", [".slots", ".queue", "_orphans", "_window"])
    def test_reserved_toplevel_never_moved(self, tmp_path, reserved):
        """Reserved names are never touched by the sweep."""
        d = tmp_path / reserved
        d.mkdir()
        # Backdate so it would normally be treated as orphan.
        old_ts = _NOW_TS - 3600
        os.utime(d, (old_ts, old_ts))

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 0
        assert d.exists()

    def test_dot_prefix_never_moved(self, tmp_path):
        """Any dot-prefixed top-level entry is skipped."""
        d = tmp_path / ".hidden"
        d.mkdir()
        old_ts = _NOW_TS - 3600
        os.utime(d, (old_ts, old_ts))

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 0

    def test_underscore_prefix_never_moved(self, tmp_path):
        """Any underscore-prefixed top-level entry is skipped."""
        d = tmp_path / "_custom"
        d.mkdir()
        old_ts = _NOW_TS - 3600
        os.utime(d, (old_ts, old_ts))

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 0


class TestPermissionError:
    def test_permission_error_on_single_workspace_logs_and_continues(self, tmp_path, capsys):
        """PermissionError on one workspace is isolated — others still get swept."""
        _make_workspace(tmp_path, "dispatch-ok", age_seconds=600)
        _make_workspace(tmp_path, "dispatch-bad", age_seconds=600)

        original_rename = os.rename

        def _patched_rename(src, dst):
            if "dispatch-bad" in str(src):
                raise PermissionError("permission denied")
            original_rename(src, dst)

        with patch.object(os, "rename", side_effect=_patched_rename):
            result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["moved"] == 1
        assert result["errors"] == 1
        logs = _collect_log(capsys)
        error_events = [e for e in logs if e["event"] == "janitor_error"]
        assert len(error_events) == 1
        assert error_events[0]["dispatch_id"] == "dispatch-bad"


class TestMissingRoot:
    def test_missing_workspace_root_does_not_raise(self, tmp_path, capsys):
        """Missing /var/lib/dispatch is caught at top level; returns zeros."""
        absent = str(tmp_path / "nonexistent")

        result = _janitor.sweep(absent, now=_NOW)

        assert result == {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0}
        logs = _collect_log(capsys)
        failed = [e for e in logs if e["event"] == "janitor_failed"]
        assert len(failed) == 1
        assert "workspace_root missing" in failed[0]["reason"]
