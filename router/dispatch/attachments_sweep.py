"""Router-side periodic attachments GC sweep (#327).

The attachments scratch root ``/var/lib/attachments/<thread_id>/`` is
mounted **read-write only into the router container** (named agents get
it read-only; workers/browser-use get nothing). The pack handler's
``attachments_sweep`` verb runs inside the *agent* container where that
mount is RO, so ``shutil.rmtree`` there throws ``PermissionError`` on
every delete. The sweep must therefore execute in the **router process**.

This module registers a singleton *system task* on the existing
scheduled-tasks scheduler — the same ``callable_ref`` + ``period_seconds``
mechanism the #163 dispatch-supervision loop uses. The scheduler imports
:func:`run_sweep` every ``period_seconds`` and invokes it in-process, in
the RW context, with no Claude session spawned.

The actual sweep logic lives in the dispatch pack
(``packs/dispatch/attachments_janitor.py``) so it stays colocated with the
ingest code (#328/#330) that shares the TTL/disk-pressure constants. We
import it dynamically — mirroring how :mod:`router.dispatch.supervision`
loads the pack's ``quota`` module — so a missing or partially-upgraded
pack dir can never kill the scheduler loop.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CALLABLE_REF = "router.dispatch.attachments_sweep:run_sweep"
SWEEP_TASK_NAME = "attachments GC sweep"
# TTL is 7 days (ATTACHMENTS_TTL_DAYS); a 6-hour cadence keeps the backlog
# from ever building while staying effectively free (a directory stat walk).
SWEEP_PERIOD_SECONDS = 6 * 3600

# Dispatch pack dir, mounted at /app/packs/dispatch in the router container
# (see docker-compose.yml). attachments_janitor lives here alongside the
# constants it shares with the ingest verbs.
_PACK_DIR = Path(__file__).resolve().parent.parent.parent / "packs" / "dispatch"


def _load_sweep_fn():
    """Import ``attachments_janitor.sweep`` from the pack dir.

    Best-effort: returns ``None`` (and logs) if the pack dir isn't present
    or importable, so a bad deploy degrades to "GC doesn't run" rather than
    "scheduler crashes".
    """
    if _PACK_DIR.is_dir() and str(_PACK_DIR) not in sys.path:
        sys.path.insert(0, str(_PACK_DIR))
    try:
        from attachments_janitor import sweep  # noqa: PLC0415

        return sweep
    except ImportError:
        logger.exception("attachments_sweep: could not import attachments_janitor from %s", _PACK_DIR)
        return None


def run_sweep(*, payload: dict | None = None, slack_client: Any = None, now: Any = None) -> dict:
    """System-task callable: run one attachments sweep in the router (RW) context.

    Signature matches the scheduler's ``_invoke_callable`` contract
    (``payload``, ``slack_client``, ``now`` are passed by keyword). The
    Slack client is unused — this task posts nothing — but must be accepted.

    Returns a non-terminal status dict (never ``{"status": "done"}``) so the
    scheduler keeps the task scheduled and re-fires it every
    ``period_seconds``.
    """
    sweep = _load_sweep_fn()
    if sweep is None:
        return {"status": "ok", "skipped": "pack_unavailable"}
    try:
        result = sweep()
    except Exception:
        logger.exception("attachments_sweep: sweep raised; keeping task scheduled")
        return {"status": "ok", "error": "sweep_failed"}
    logger.info(
        "attachments_sweep: removed=%s kept=%s errors=%s bytes_freed=%s",
        result.get("removed"),
        result.get("kept"),
        result.get("errors"),
        result.get("bytes_freed"),
    )
    return {"status": "ok", **result}


def register_attachments_sweep(
    store,
    *,
    agent_name: str,
    period_seconds: int = SWEEP_PERIOD_SECONDS,
) -> Any:
    """Idempotently register the singleton attachments-sweep system task.

    Safe to call on every router boot: if a task already exists under
    :data:`CALLABLE_REF` (it persists in the SQLite store across restarts)
    we return the existing row untouched rather than creating a duplicate.

    ``agent_name`` only determines which Slack client the scheduler resolves
    for the tick; the sweep posts nothing, but the scheduler requires a
    non-None client to invoke the callable, so pass a configured agent.
    """
    existing = store.list_by_callable_ref(CALLABLE_REF)
    if existing:
        logger.debug("attachments_sweep: system task already registered (%s)", existing[0].task_id)
        return existing[0]
    task = store.create_system_task(
        agent_name=agent_name,
        name=SWEEP_TASK_NAME,
        callable_ref=CALLABLE_REF,
        payload={},
        period_seconds=period_seconds,
    )
    logger.info(
        "attachments_sweep: registered system task task_id=%s period=%ss agent=%s",
        task.task_id,
        period_seconds,
        agent_name,
    )
    return task
