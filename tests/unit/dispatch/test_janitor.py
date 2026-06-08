"""Unit tests for packs.dispatch.janitor — orphan workspace sweep."""

from __future__ import annotations

import json
import os
import sys
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

import janitor as _janitor  # noqa: E402

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

    def test_moved_dir_preserves_halt_reason(self, tmp_path):
        """#255 — the halt_reason forensic file must survive the move to
        _orphans/ so a premature-kill can still be diagnosed post-mortem."""
        ws = _make_workspace(tmp_path, "dispatch-killed", age_seconds=600, has_exitcode=True)
        reason = '{"reason":"budget_overrun","detail":"elapsed 120s exceeds budget 60s"}'
        (ws / "halt_reason").write_text(reason)
        old_ts = _NOW_TS - 600
        os.utime(ws, (old_ts, old_ts))

        _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        orphans = list((tmp_path / "_orphans").iterdir())
        assert len(orphans) == 1
        preserved = orphans[0] / "halt_reason"
        assert preserved.exists(), "halt_reason must not be deleted when orphaning"
        assert json.loads(preserved.read_text())["reason"] == "budget_overrun"


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

        assert result == {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0, "slots_reclaimed": 0}
        logs = _collect_log(capsys)
        summary = [e for e in logs if e["event"] == "janitor_summary"]
        assert len(summary) == 1
        assert summary[0] == {
            "event": "janitor_summary",
            "moved": 0,
            "aged_out": 0,
            "skipped_live": 0,
            "errors": 0,
            "slots_reclaimed": 0,
        }


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

        assert result == {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0, "slots_reclaimed": 0}
        logs = _collect_log(capsys)
        failed = [e for e in logs if e["event"] == "janitor_failed"]
        assert len(failed) == 1
        assert "workspace_root missing" in failed[0]["reason"]


def _make_slot(root: Path, slot_idx: int, dispatch_id: str) -> Path:
    """Create a fake slot-lock file holding the given dispatch_id."""
    slots_dir = root / ".slots"
    slots_dir.mkdir(exist_ok=True)
    slot_path = slots_dir / f"slot-{slot_idx}"
    slot_path.write_text(dispatch_id)
    return slot_path


class TestSlotReclamation:
    def test_stale_slot_with_exitcode_is_reclaimed(self, tmp_path, capsys):
        """Slot whose workspace has exitcode → slot unlinked (exitcode check)."""
        _make_workspace(tmp_path, "dispatch-dead", age_seconds=600, has_exitcode=True)
        slot = _make_slot(tmp_path, 0, "dispatch-dead")

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["slots_reclaimed"] >= 1
        assert not slot.exists(), "stale slot file must be removed"
        logs = _collect_log(capsys)
        reclaim_events = [e for e in logs if e["event"] == "slot_reclaimed"]
        assert len(reclaim_events) >= 1
        ev = reclaim_events[0]
        assert ev["slot"] == "slot-0"
        assert ev["dispatch_id"] == "dispatch-dead"
        assert ev["reason"] == "exitcode_present"

    def test_stale_slot_with_missing_workspace_is_reclaimed(self, tmp_path, capsys):
        """Slot whose workspace doesn't exist → slot unlinked (orphan check)."""
        slot = _make_slot(tmp_path, 1, "dispatch-gone")
        # No workspace directory created — it's already gone.

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["slots_reclaimed"] >= 1
        assert not slot.exists(), "stale slot file must be removed"
        logs = _collect_log(capsys)
        reclaim_events = [e for e in logs if e["event"] == "slot_reclaimed"]
        assert len(reclaim_events) >= 1
        ev = reclaim_events[0]
        assert ev["slot"] == "slot-1"
        assert ev["dispatch_id"] == "dispatch-gone"
        assert ev["reason"] == "workspace_missing"

    def test_live_slot_with_present_workspace_and_no_exitcode_is_preserved(self, tmp_path, capsys):
        """Slot whose workspace is live (present, no exitcode) → slot kept."""
        _make_workspace(tmp_path, "dispatch-live", age_seconds=10)
        slot = _make_slot(tmp_path, 0, "dispatch-live")

        result = _janitor.sweep(str(tmp_path), now=_NOW, grace_seconds=60)

        assert result["slots_reclaimed"] == 0
        assert slot.exists(), "live slot file must not be removed"
        logs = _collect_log(capsys)
        reclaim_events = [e for e in logs if e["event"] == "slot_reclaimed"]
        assert reclaim_events == []
