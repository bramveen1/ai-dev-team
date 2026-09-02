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

from constants import HEARTBEAT_INTERVAL, ORPHAN_TTL_DAYS, POOL_SLOTS_DIR_NAME, RESERVED_TOPLEVEL, STARTUP_GRACE_SECONDS

logger = logging.getLogger("dispatch.janitor")

_ORPHANS_DIR = "_orphans"
# Timestamp format used in orphan destination names; no colons so it's
# safe as a directory-name component on every filesystem.
_TS_FMT = "%Y%m%dT%H%M%SZ"
# Persisted skip-set of orphans whose rmtree has already failed once (#870)
# — e.g. a root-owned subtree Docker left behind that uid 1000 can never
# remove. Keeps a stuck entry from re-erroring on every sweep forever.
_UNRECLAIMABLE_FILE = "_unreclaimable.json"


def _emit(record: dict) -> None:
    # Write to stderr so the structured log lines don't pollute the
    # handler's stdout verb response (which must be a single JSON object).
    print(json.dumps(record), file=sys.stderr, flush=True)


def _reclaim_slots_by_condition(
    slots_dir: Path,
    workspace_root: Path,
    *,
    exitcode_check: bool,
) -> int:
    """Scan slot files and unlink those whose dispatch is no longer live.

    When *exitcode_check* is True, unlinks slots whose workspace has an
    ``exitcode`` file (dispatch is terminal but slot was never released).
    When False, unlinks slots whose workspace directory no longer exists
    under *workspace_root* (workspace was moved away or never existed).

    Returns the count of slots reclaimed.
    """
    if not slots_dir.exists():
        return 0
    reclaimed = 0
    for slot_path in sorted(slots_dir.iterdir()):
        if not (slot_path.is_file() and slot_path.name.startswith("slot-")):
            continue
        try:
            dispatch_id = slot_path.read_text().strip()
        except OSError:
            continue
        if not dispatch_id:
            continue
        if exitcode_check:
            stale = (workspace_root / dispatch_id / "exitcode").exists()
            reason = "exitcode_present"
        else:
            stale = not (workspace_root / dispatch_id).exists()
            reason = "workspace_missing"
        if not stale:
            continue
        try:
            slot_path.unlink()
            _emit(
                {
                    "event": "slot_reclaimed",
                    "slot": slot_path.name,
                    "dispatch_id": dispatch_id,
                    "reason": reason,
                }
            )
            reclaimed += 1
        except OSError as e:
            _emit({"event": "janitor_error", "slot": slot_path.name, "dispatch_id": dispatch_id, "reason": str(e)})
    return reclaimed


def _load_unreclaimable(orphans_dir: Path) -> dict:
    """Load the persisted rmtree-failure skip-set, tolerating a missing or corrupt file."""
    try:
        data = json.loads((orphans_dir / _UNRECLAIMABLE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_unreclaimable(orphans_dir: Path, unreclaimable: dict) -> None:
    try:
        (orphans_dir / _UNRECLAIMABLE_FILE).write_text(json.dumps(unreclaimable))
    except OSError as e:
        _emit({"event": "janitor_error", "reason": f"cannot persist {_UNRECLAIMABLE_FILE}: {e}"})


def sweep_orphans(
    workspace_root: str = "/var/lib/dispatch",
    *,
    now: datetime | None = None,
    orphan_ttl_days: int = ORPHAN_TTL_DAYS,
) -> dict:
    """Age out stale ``_orphans/`` entries.

    Lightweight best-effort sweep safe to call on every auto_dispatch tick.
    Returns ``{aged_out: int, errors: int}``.

    Uses ``ORPHAN_TTL_DAYS`` from ``constants.py`` as the single TTL source;
    callers should not hard-code a second value.

    An entry whose ``rmtree`` fails (e.g. a root-owned subtree Docker left
    behind — #870) is recorded in a persisted skip-set
    (``_orphans/_unreclaimable.json``) and is not retried on later sweeps;
    ``router/entrypoint.sh`` reclaims it for real via a recursive chown on
    the next container start. Already-known entries are reported once per
    sweep as a single aggregated ``janitor_unreclaimable`` warning instead
    of a per-entry ``janitor_error``, so a genuinely new failure still
    stands out.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    ttl_seconds = orphan_ttl_days * 86400
    aged_out = errors = 0
    root = Path(workspace_root)
    orphans_dir = root / _ORPHANS_DIR
    if not orphans_dir.exists():
        return {"aged_out": 0, "errors": 0}

    unreclaimable = _load_unreclaimable(orphans_dir)
    unreclaimable_dirty = False
    skipped_unreclaimable = 0

    try:
        for entry in list(orphans_dir.iterdir()):
            if not entry.is_dir():
                continue
            raw_name = entry.name
            if raw_name in unreclaimable:
                skipped_unreclaimable += 1
                continue
            dispatch_id = raw_name[17:] if len(raw_name) > 17 else raw_name
            try:
                age = now_ts - entry.stat().st_mtime
                if age >= ttl_seconds:
                    shutil.rmtree(entry)
                    _emit({"event": "orphan_aged_out", "dispatch_id": dispatch_id, "age_seconds": int(age)})
                    aged_out += 1
            except (OSError, PermissionError) as e:
                _emit({"event": "janitor_error", "dispatch_id": dispatch_id, "reason": str(e)})
                errors += 1
                unreclaimable[raw_name] = {
                    "dispatch_id": dispatch_id,
                    "first_seen": now.isoformat(),
                    "reason": str(e),
                }
                unreclaimable_dirty = True
    except (OSError, PermissionError) as e:
        _emit({"event": "janitor_failed", "reason": f"cannot scan _orphans: {e}"})

    if unreclaimable_dirty:
        _save_unreclaimable(orphans_dir, unreclaimable)
    if skipped_unreclaimable:
        _emit({"event": "janitor_unreclaimable", "count": skipped_unreclaimable})

    return {"aged_out": aged_out, "errors": errors}


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

    moved = aged_out = skipped_live = errors = slots_reclaimed = 0
    root = Path(workspace_root)

    if not root.exists():
        _emit({"event": "janitor_failed", "reason": f"workspace_root missing: {workspace_root}"})
        return {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0, "slots_reclaimed": 0}

    orphans_dir = root / _ORPHANS_DIR
    try:
        orphans_dir.mkdir(exist_ok=True)
    except OSError as e:
        _emit({"event": "janitor_failed", "reason": f"cannot create _orphans dir: {e}"})
        return {"moved": 0, "aged_out": 0, "skipped_live": 0, "errors": 0, "slots_reclaimed": 0}

    now_ts = now.timestamp()

    # ── Age out stale _orphans/ entries ───────────────────────────────────
    orphan_result = sweep_orphans(workspace_root, now=now, orphan_ttl_days=orphan_ttl_days)
    aged_out += orphan_result["aged_out"]
    errors += orphan_result["errors"]

    # ── Check 1: reclaim slots whose workspace has an exitcode (early) ───────
    slots_dir = root / POOL_SLOTS_DIR_NAME
    slots_reclaimed += _reclaim_slots_by_condition(slots_dir, root, exitcode_check=True)

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
                "slots_reclaimed": slots_reclaimed,
            }
        )
        return {
            "moved": moved,
            "aged_out": aged_out,
            "skipped_live": skipped_live,
            "errors": errors,
            "slots_reclaimed": slots_reclaimed,
        }

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

            if not has_exitcode:
                if age < grace_seconds:
                    # Within the startup race window — a handler may be actively
                    # creating this workspace.  Leave it alone.
                    skipped_live += 1
                    continue
                # Consult the heartbeat file: babysit touches it every
                # HEARTBEAT_INTERVAL seconds while the dispatch is running.
                # Directory mtime is frozen during long agent turns (no
                # top-level file churn), so the heartbeat is the only
                # reliable liveness signal for concurrent dispatches.
                heartbeat_path = entry / "heartbeat"
                try:
                    hb_mtime = heartbeat_path.stat().st_mtime
                    heartbeat_age = now_ts - hb_mtime
                except OSError:
                    heartbeat_age = float("inf")
                if heartbeat_age < 3 * HEARTBEAT_INTERVAL:
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

    # ── Check 2: reclaim slots whose workspace no longer exists (post-move) ──
    slots_reclaimed += _reclaim_slots_by_condition(slots_dir, root, exitcode_check=False)

    _emit(
        {
            "event": "janitor_summary",
            "moved": moved,
            "aged_out": aged_out,
            "skipped_live": skipped_live,
            "errors": errors,
            "slots_reclaimed": slots_reclaimed,
        }
    )
    return {
        "moved": moved,
        "aged_out": aged_out,
        "skipped_live": skipped_live,
        "errors": errors,
        "slots_reclaimed": slots_reclaimed,
    }
