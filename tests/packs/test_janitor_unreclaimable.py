"""Regression test for #870 — janitor must not re-error unreclaimable orphans.

Drives the real ``janitor.sweep()`` entry point (not the private
``sweep_orphans`` helper) so the persisted skip-set and the aggregated
warning are exercised exactly as production calls them. ``shutil.rmtree``
is monkeypatched to raise ``PermissionError`` for aged-out orphans,
simulating a root-owned subtree Docker left behind (CI runs as non-root,
so a real root-owned file can't be created here).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_PACK_DIR = str(Path(__file__).resolve().parents[2] / "packs" / "dispatch")
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

import janitor as _janitor  # noqa: E402

pytestmark = pytest.mark.unit

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_TS = _NOW.timestamp()
_TTL_DAYS = 7


def _make_orphan(root: Path, name: str, *, age_seconds: int) -> Path:
    """Create a fake _orphans/ entry with a backdated mtime, past TTL by default."""
    orphans_dir = root / "_orphans"
    orphans_dir.mkdir(exist_ok=True)
    entry = orphans_dir / name
    entry.mkdir()
    old_ts = _NOW_TS - age_seconds
    os.utime(entry, (old_ts, old_ts))
    return entry


def _collect_log(capsys) -> list[dict]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.strip()]


class TestUnreclaimableOrphans:
    def test_full_lifecycle(self, tmp_path, capsys):
        stuck = _make_orphan(tmp_path, "20240101T000000Z-dispatch-stuck", age_seconds=8 * 86400)

        with patch.object(_janitor.shutil, "rmtree", side_effect=PermissionError("permission denied")):
            # ── Round 1: first-ever failure surfaces and is persisted ──────
            result = _janitor.sweep(str(tmp_path), now=_NOW, orphan_ttl_days=_TTL_DAYS)

            assert result["errors"] == 1
            assert stuck.exists(), "rmtree failed — entry must remain in place"

            unreclaimable_path = tmp_path / "_orphans" / "_unreclaimable.json"
            assert unreclaimable_path.exists()
            recorded = json.loads(unreclaimable_path.read_text())
            assert "20240101T000000Z-dispatch-stuck" in recorded

            logs = _collect_log(capsys)
            error_events = [
                e for e in logs if e["event"] == "janitor_error" and e.get("dispatch_id") == "dispatch-stuck"
            ]
            assert len(error_events) == 1
            assert not any(e["event"] == "janitor_unreclaimable" for e in logs)

            # ── Round 2: known-unreclaimable orphan is skipped, not re-errored ──
            result = _janitor.sweep(str(tmp_path), now=_NOW, orphan_ttl_days=_TTL_DAYS)

            assert result["errors"] == 0
            assert stuck.exists()

            logs = _collect_log(capsys)
            assert not any(e["event"] == "janitor_error" for e in logs), "known orphan must not re-error"
            unreclaimable_events = [e for e in logs if e["event"] == "janitor_unreclaimable"]
            assert len(unreclaimable_events) == 1
            assert unreclaimable_events[0]["count"] == 1

            # ── Round 3: a brand-new failing orphan still surfaces on first sight ──
            new_stuck = _make_orphan(tmp_path, "20240102T000000Z-dispatch-newstuck", age_seconds=8 * 86400)

            result = _janitor.sweep(str(tmp_path), now=_NOW, orphan_ttl_days=_TTL_DAYS)

            assert result["errors"] == 1
            assert new_stuck.exists()

            logs = _collect_log(capsys)
            error_events = [
                e for e in logs if e["event"] == "janitor_error" and e.get("dispatch_id") == "dispatch-newstuck"
            ]
            assert len(error_events) == 1, "a first-seen failure must still surface"
            unreclaimable_events = [e for e in logs if e["event"] == "janitor_unreclaimable"]
            assert len(unreclaimable_events) == 1
            assert unreclaimable_events[0]["count"] == 1, "aggregate only covers already-known entries"

            recorded = json.loads(unreclaimable_path.read_text())
            assert "20240101T000000Z-dispatch-stuck" in recorded
            assert "20240102T000000Z-dispatch-newstuck" in recorded

    def test_zero_unreclaimable_orphans_is_byte_identical(self, tmp_path, capsys):
        """No rmtree failures ever occur → no skip-set file, no new log events."""
        _make_orphan(tmp_path, "20240101T000000Z-dispatch-clean", age_seconds=8 * 86400)

        result = _janitor.sweep(str(tmp_path), now=_NOW, orphan_ttl_days=_TTL_DAYS)

        assert result["aged_out"] == 1
        assert result["errors"] == 0
        assert not (tmp_path / "_orphans" / "_unreclaimable.json").exists()

        logs = _collect_log(capsys)
        assert not any(e["event"] in ("janitor_error", "janitor_unreclaimable") for e in logs)
