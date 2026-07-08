"""Slot-pool and FIFO-queue primitives for the dispatch pack.

Single source of truth for the lock-file protocol. Before this module
existed, ``handler.py`` and ``babysit.py`` carried divergent copies of the
release functions — a correctness risk for a concurrency protocol (#505
recycled-index races were fixed in one copy and hand-mirrored in the
other). Both now delegate here, and ``router.dispatch.supervision`` loads
this module directly instead of executing all of ``handler.py`` for one
function.

The protocol: a slot is a ``slot-<idx>`` file under the slots dir whose
mere existence is the lock (``O_CREAT | O_EXCL``); the file body records
the owning ``dispatch_id`` for diagnostics and owner-checked release.
Queue tickets are timestamp-named files whose sort order is FIFO order.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from constants import POOL_SLOTS_DIR_NAME  # noqa: F401 — re-exported for loaders

# Hard concurrency cap. At most POOL_SIZE ``claude -p`` subprocesses run
# in parallel; excess requests queue FIFO (design doc §Concurrency).
POOL_SIZE = 3

# Hidden subdir under the workspace root for queue bookkeeping. Leading
# dot keeps it out of list_dispatch_ids() and the dispatch namespace.
POOL_QUEUE_DIR_NAME = ".queue"


def slots_dir(root: Path) -> Path:
    """Return (and ensure) the pool slot-lock directory under ``root``."""
    d = root / POOL_SLOTS_DIR_NAME
    d.mkdir(exist_ok=True, mode=0o700)
    return d


def queue_dir(root: Path) -> Path:
    """Return (and ensure) the FIFO queue ticket directory under ``root``."""
    d = root / POOL_QUEUE_DIR_NAME
    d.mkdir(exist_ok=True, mode=0o700)
    return d


def try_acquire_slot(slots_dir: Path, dispatch_id: str) -> int | None:
    """Atomically claim the lowest-numbered free slot via ``O_CREAT|O_EXCL``.

    Returns the slot index (0-based) on success, ``None`` when all
    ``POOL_SIZE`` slots are occupied. The slot file stores the owning
    ``dispatch_id`` for diagnostics; the file's mere existence is the
    lock.
    """
    for i in range(POOL_SIZE):
        slot_path = slots_dir / f"slot-{i}"
        try:
            fd = os.open(str(slot_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, dispatch_id.encode())
            finally:
                os.close(fd)
            return i
        except FileExistsError:
            continue
        except OSError:
            slot_path.unlink(missing_ok=True)
            raise
    return None


def release_slot(slots_dir: Path, slot_idx: int) -> None:
    """Release a slot by removing its lock file. Idempotent.

    NOTE: releasing by index is unsafe against recycled-index races — if
    the caller's slot has been freed and reacquired by a different
    dispatch, the unlink deletes the wrong owner's lock. Production
    release paths should prefer :func:`release_slot_for_dispatch`.
    """
    slot_path = slots_dir / f"slot-{slot_idx}"
    try:
        slot_path.unlink()
    except FileNotFoundError:
        pass


def release_slot_for_dispatch(slots_dir: Path, dispatch_id: str) -> None:
    """Release whichever slot file holds this dispatch_id. Idempotent.

    Used by cancel/exit paths that know the dispatch_id but not the slot
    index. Safe against recycled-index races (#505): if the index has
    been recycled and a different dispatch now holds it, this is a no-op
    rather than a cross-dispatch delete.
    """
    if not slots_dir.exists():
        return
    for p in slots_dir.iterdir():
        if not (p.is_file() and p.name.startswith("slot-")):
            continue
        try:
            if p.read_text().strip() == dispatch_id:
                p.unlink(missing_ok=True)
                return
        except OSError:
            continue


def count_active_slots(slots_dir: Path) -> int:
    """Return the number of slot-lock files currently present."""
    if not slots_dir.exists():
        return 0
    return sum(1 for p in slots_dir.iterdir() if p.is_file() and p.name.startswith("slot-"))


def queue_ticket(queue_dir: Path, dispatch_id: str) -> Path:
    """Create a FIFO queue ticket. The sortable name (``<ns_timestamp>-<id>``)
    determines FIFO order when multiple dispatches queue simultaneously.
    """
    ticket_name = f"{time.time_ns():020d}-{dispatch_id}"
    ticket_path = queue_dir / ticket_name
    ticket_path.write_text(dispatch_id)
    return ticket_path


def queue_position(queue_dir: Path, ticket_name: str) -> int:
    """Return how many tickets are ahead of ours (0 = front of queue)."""
    try:
        tickets = sorted(p.name for p in queue_dir.iterdir() if p.is_file())
    except (FileNotFoundError, OSError):
        return 0
    try:
        return tickets.index(ticket_name)
    except ValueError:
        return 0  # ticket removed or not found — treat as front
