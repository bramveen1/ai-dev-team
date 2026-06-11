"""Attachments scratch-dir sweep and disk-pressure guard (#327).

Manages /var/lib/attachments/<thread_id>/ lifecycle:

- ``sweep``: removes thread dirs whose mtime is older than the TTL.
  Mtime is bumped by the router on every message in the thread, so
  active threads are naturally preserved.
- ``check_disk_pressure``: returns True when the attachments volume is
  over a configurable usage threshold.  Ingest callers should skip
  writing new content when this returns True.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from constants import ATTACHMENTS_DISK_PRESSURE_PCT, ATTACHMENTS_ROOT, ATTACHMENTS_TTL_DAYS

logger = logging.getLogger("dispatch.attachments_janitor")


def _emit(record: dict) -> None:
    print(json.dumps(record), file=sys.stderr, flush=True)


def _dir_size_bytes(path: Path) -> int:
    """Return total bytes consumed by all regular files under *path*."""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def check_disk_pressure(
    attachments_root: str = ATTACHMENTS_ROOT,
    threshold_pct: int = ATTACHMENTS_DISK_PRESSURE_PCT,
) -> bool:
    """Return True when disk usage at *attachments_root* is >= *threshold_pct*.

    Emits an ``attachments_disk_pressure`` log record when over threshold.
    Never raises; returns False on any stat failure so callers never crash.
    """
    root = Path(attachments_root)
    if not root.exists():
        return False
    try:
        usage = shutil.disk_usage(str(root))
        used_pct = usage.used / usage.total * 100
        if used_pct >= threshold_pct:
            _emit(
                {
                    "event": "attachments_disk_pressure",
                    "used_pct": round(used_pct, 1),
                    "threshold_pct": threshold_pct,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                }
            )
            return True
        return False
    except OSError as e:
        logger.warning("Failed to check disk usage for %s: %s", attachments_root, e)
        return False


def sweep(
    attachments_root: str = ATTACHMENTS_ROOT,
    *,
    now: datetime | None = None,
    ttl_days: int = ATTACHMENTS_TTL_DAYS,
) -> dict:
    """Remove thread dirs under *attachments_root* whose mtime is older than *ttl_days*.

    Returns ``{removed: int, kept: int, errors: int, bytes_freed: int}``.

    The sweep is best-effort: a failure on one thread dir is logged in
    ``errors`` but does not abort processing of the remaining dirs.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    removed = kept = errors = 0
    bytes_freed = 0
    root = Path(attachments_root)

    if not root.exists():
        _emit({"event": "attachments_sweep_failed", "reason": f"root missing: {attachments_root}"})
        return {"removed": 0, "kept": 0, "errors": 0, "bytes_freed": 0}

    now_ts = now.timestamp()
    ttl_seconds = ttl_days * 86400

    try:
        entries = list(root.iterdir())
    except OSError as e:
        _emit({"event": "attachments_sweep_failed", "reason": f"cannot scan root: {e}"})
        return {"removed": 0, "kept": 0, "errors": 0, "bytes_freed": 0}

    for entry in entries:
        if not entry.is_dir():
            continue
        thread_id = entry.name
        try:
            age = now_ts - entry.stat().st_mtime
            if age >= ttl_seconds:
                size = _dir_size_bytes(entry)
                shutil.rmtree(entry)
                bytes_freed += size
                removed += 1
                _emit(
                    {
                        "event": "attachments_thread_removed",
                        "thread_id": thread_id,
                        "age_seconds": int(age),
                        "bytes_freed": size,
                    }
                )
            else:
                kept += 1
        except (OSError, PermissionError) as e:
            _emit({"event": "attachments_sweep_error", "thread_id": thread_id, "reason": str(e)})
            errors += 1

    _emit(
        {
            "event": "attachments_sweep_summary",
            "removed": removed,
            "kept": kept,
            "errors": errors,
            "bytes_freed": bytes_freed,
        }
    )
    return {"removed": removed, "kept": kept, "errors": errors, "bytes_freed": bytes_freed}
