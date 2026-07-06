"""Live dispatch-slot checks (one bug in flight at a time) + orphan sweep.

Reads the on-disk dispatch state (``router.dispatch.state``) to answer
"is anything running right now?" and reaps stale slots so a force-killed
worker cannot wedge the loop forever.
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)


def _get_in_flight_issue_nums(dispatch_root_override: str | None = None) -> set[int]:
    """Return issue numbers of all currently in-flight (alive) dispatches.

    Stale slots (dead heartbeat or past max-age backstop) are reaped to
    ``_orphans/`` so a force-killed worker cannot block new dispatches.
    """
    from router.dispatch import state as dstate

    result: set[int] = set()
    now = time.time()
    for dispatch_id in dstate.list_dispatch_ids(root=dispatch_root_override):
        if dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root_override) is not None:
            continue
        if dstate.is_dispatch_stale(dispatch_id, root=dispatch_root_override, now=now):
            logger.info("auto_dispatch: reaped stale slot %s", dispatch_id)
            dstate.reap_stale_dispatch(dispatch_id, root=dispatch_root_override, now=now)
            continue
        issue_url = dstate.read_field(dispatch_id, dstate.FIELD_ISSUE_URL, root=dispatch_root_override) or ""
        m = re.search(r"/issues/(\d+)$", issue_url)
        if m:
            result.add(int(m.group(1)))
    return result


def _has_any_in_flight_dispatch(dispatch_root_override: str | None = None) -> bool:
    """True when at least one dispatch has no exitcode AND is actually alive.

    Stale slots (dead heartbeat or past max-age backstop) are reaped to
    ``_orphans/`` so a force-killed worker cannot wedge the loop forever.
    """
    from router.dispatch import state as dstate

    now = time.time()
    for dispatch_id in dstate.list_dispatch_ids(root=dispatch_root_override):
        if dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root_override) is not None:
            continue
        if dstate.is_dispatch_stale(dispatch_id, root=dispatch_root_override, now=now):
            logger.info("auto_dispatch: reaped stale slot %s", dispatch_id)
            dstate.reap_stale_dispatch(dispatch_id, root=dispatch_root_override, now=now)
            continue
        return True
    return False


def _run_periodic_orphan_sweep(workspace_root: str | None = None) -> None:
    """Age out stale ``_orphans/`` entries; best-effort, never raises.

    Delegates to ``janitor.sweep_orphans`` (the same function called by the
    startup sweep) so ``ORPHAN_TTL_DAYS`` from ``packs/dispatch/constants.py``
    remains the single TTL source.  Importing dynamically mirrors the pattern
    used by ``router.dispatch.attachments_sweep``.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _pack_dir = _Path(__file__).resolve().parents[2] / "packs" / "dispatch"
    if _pack_dir.is_dir() and str(_pack_dir) not in _sys.path:
        _sys.path.insert(0, str(_pack_dir))
    try:
        from janitor import sweep_orphans  # noqa: PLC0415

        root = workspace_root or "/var/lib/dispatch"
        result = sweep_orphans(root)
        if result.get("aged_out") or result.get("errors"):
            logger.info("auto_dispatch: orphan sweep aged_out=%d errors=%d", result["aged_out"], result["errors"])
    except Exception:
        logger.exception("auto_dispatch: periodic orphan sweep failed (non-fatal)")
