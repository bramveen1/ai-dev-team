"""Startup sweep for orphaned dispatch workspaces (D-6 janitor).

Runs once per Sam container start, before any new dispatch is accepted,
to move crash-left-behind workspaces into ``_orphans/`` and trim stale
orphan entries.  Call :func:`sweep` directly; the handler wires it
lazily via a process-level sentinel.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from constants import ORPHAN_TTL_DAYS, RESERVED_TOPLEVEL, STARTUP_GRACE_SECONDS

logger = logging.getLogger("dispatch.janitor")

_ORPHANS_DIR = "_orphans"
# Timestamp format used in orphan destination names; no colons so it's
# safe as a directory-name component on every filesystem.
_TS_FMT = "%Y%m%dT%H%M%SZ"


def _emit(record: dict) -> None:
    # Write to stderr so the structured log lines don't pollute the
    # handler's stdout verb response (which must be a single JSON object).
    print(json.dumps(record), file=sys.stderr, flush=True)


def sweep(
    workspace_root: str = "/var/lib/dispatch",
    *,
    now: datetime | None = None,
    grace_seconds: int = STARTUP_GRACE_SECONDS,
    orphan_ttl_days: int = ORPHAN_TTL_DAYS,
) -> dict:
    """Sweep *workspace_root* for orphaned dispatch workspaces.

    Returns a summary: {moved: int, aged_out: int, skipped_live: int, errors: int}.

    The entire sweep is best-effort — a PermissionError on one entry is
    logged and counted in ``errors`` but does not abort the sweep.
    A missing ``workspace_root`` or an unrecoverable top-level failure is
    logged as ``janitor_failed`` and returns all-zero counters so the
    caller can still proceed.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    moved = aged_out = skipped_live = errors = 0
    root = Path(workspace_root)

    if not root.exists():
        _emit({"event": "janitor_failed", "reason": f"workspace_root missing: {workspace_root}"})
        return {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0}

    orphans_dir = root / _ORPHANS_DIR
    try:
        orphans_dir.mkdir(exist_ok=True)
    except OSError as e:
        _emit({"event": "janitor_failed", "reason": f"cannot create _orphans dir: {e}"})
        return {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0}

    now_ts = now.timestamp()
    ttl_seconds = orphan_ttl_days * 86400

    # ── Age out stale _orphans/ entries ───────────────────────────────────
    try:
        for entry in list(orphans_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                age = now_ts - entry.stat().st_mtime
                if age >= ttl_seconds:
                    # Parse original dispatch_id from "<ts>-<dispatch_id>".
                    # The timestamp prefix is 16 chars + 1 separator dash = 17.
                    raw_name = entry.name
                    dispatch_id = raw_name[17:] if len(raw_name) > 17 else raw_name
                    shutil.rmtree(entry)
                    _emit({"event": "orphan_aged_out", "dispatch_id": dispatch_id, "age_seconds": int(age)})
                    aged_out += 1
            except (OSError, PermissionError) as e:
                _emit({"event": "janitor_error", "dispatch_id": entry.name, "reason": str(e)})
                errors += 1
    except (OSError, PermissionError) as e:
        # Can't scan _orphans — log and continue to main sweep.
        _emit({"event": "janitor_failed", "reason": f"cannot scan _orphans: {e}"})

    # ── Scan workspace root for orphaned dispatch dirs ────────────────────
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError) as e:
        _emit({"event": "janitor_failed", "reason": f"cannot scan workspace root: {e}"})
        _emit(
            {
                "event": "janitor_summary",
                "moved": moved,
                "aged_out": aged_out,
                "skipped_live": skipped_live,
                "errors": errors,
            }
        )
        return {"moved": moved, "aged_out": aged_out, "skipped_live": skipped_live, "errors": errors}

    for entry in entries:
        if not entry.is_dir():
            continue

        name = entry.name
        # Skip reserved names and any dot/underscore-prefixed entries
        # (defensive default per the issue spec).
        if name in RESERVED_TOPLEVEL or name.startswith((".", "_")):
            continue

        dispatch_id = name
        try:
            stat = entry.stat()
            age = now_ts - stat.st_mtime
            has_exitcode = (entry / "exitcode").exists()

            if not has_exitcode and age < grace_seconds:
                # Within the startup race window — a handler may be actively
                # creating this workspace.  Leave it alone.
                skipped_live += 1
                continue

            # Move to _orphans/<UTC-ts>-<dispatch_id>/ (rename, same volume).
            ts_str = now.strftime(_TS_FMT)
            dest = orphans_dir / f"{ts_str}-{dispatch_id}"
            os.rename(entry, dest)
            _emit(
                {
                    "event": "orphan_moved",
                    "dispatch_id": dispatch_id,
                    "age_seconds": int(age),
                    "had_exitcode": has_exitcode,
                }
            )
            moved += 1
        except (OSError, PermissionError) as e:
            _emit({"event": "janitor_error", "dispatch_id": dispatch_id, "reason": str(e)})
            errors += 1

    _emit(
        {
            "event": "janitor_summary",
            "moved": moved,
            "aged_out": aged_out,
            "skipped_live": skipped_live,
            "errors": errors,
        }
    )
    return {"moved": moved, "aged_out": aged_out, "skipped_live": skipped_live, "errors": errors}
