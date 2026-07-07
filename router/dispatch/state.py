"""Sidecar state files for in-flight dispatches.

Layout under ``/var/lib/dispatch/<dispatch_id>/``:

| File             | Writer                                | When                              |
|------------------|---------------------------------------|-----------------------------------|
| ``pid``              | handler                               | once at launch (babysit pid)      |
| ``started_at``       | handler                               | once at launch (ISO-8601)         |
| ``run_started_at``   | handler                               | after slot acquired (ISO-8601)    |
| ``budget``           | handler                               | once at launch (seconds, int)     |
| ``channel``      | handler                               | once at launch                    |
| ``thread_ts``    | handler                               | once at launch                    |
| ``agent``        | handler                               | once at launch                    |
| ``issue_url``    | handler                               | once at launch (informational)    |
| ``model``        | handler                               | once at launch (informational)    |
| ``persona``      | handler                               | once at launch (informational)    |
| ``last_event``   | babysit                               | per tick while subprocess runs    |
| ``last_tool``    | babysit                               | per tick while subprocess runs    |
| ``cost``         | babysit                               | per tick (``total_cost_usd``)     |
| ``exitcode``     | babysit (normal) or supervisor (synth)| exactly once at terminal          |
| ``halt_marker``  | kill_command                          | when ``/kill`` matches a dispatch |
| ``halt_reason``  | supervisor / kill_command             | JSON forensic record on any halt  |
| ``timeout_marker`` | supervisor                          | when budget is exceeded           |
| ``pr_url``       | babysit / handler                     | optional, on PR open              |
| ``transcript.jsonl`` | babysit                           | append-only stream of CLI events  |

All files are plain text — single line, no trailing whitespace — except
``transcript.jsonl`` which is line-delimited JSON. Writes go through
:func:`write_field` which renames a ``.tmp`` over the destination so
concurrent reads never see a half-written file.

The container path is configurable via ``DISPATCH_WORKSPACE_ROOT`` so
unit tests can point it at a ``tmp_path`` fixture.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from router.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

DISPATCH_ROOT_ENV = "DISPATCH_WORKSPACE_ROOT"
DEFAULT_DISPATCH_ROOT = "/var/lib/dispatch"

# Handler-written fields (one-shot at launch).
FIELD_PID = "pid"
FIELD_STARTED_AT = "started_at"
# Stamped after slot acquisition so queue-wait time is excluded from
# the runtime budget clock (issue #496).
FIELD_RUN_STARTED_AT = "run_started_at"
FIELD_BUDGET = "budget"
FIELD_CHANNEL = "channel"
FIELD_THREAD_TS = "thread_ts"
FIELD_AGENT = "agent"
FIELD_ISSUE_URL = "issue_url"
FIELD_MODEL = "model"
FIELD_PERSONA = "persona"

# Babysit-written fields (per-tick while subprocess runs).
FIELD_LAST_EVENT = "last_event"
FIELD_LAST_TOOL = "last_tool"
FIELD_COST = "cost"
FIELD_PR_URL = "pr_url"
FIELD_HEARTBEAT = "heartbeat"
# JSON blob written alongside last_event when event type is rate_limit_event.
# Shape: the raw ``rate_limit_info`` sub-object from the CLI event payload.
FIELD_LAST_RATE_LIMIT_INFO = "last_rate_limit_info"

# Age threshold for heartbeat_alive(). Babysit touches every 15 s, so
# 45 s (3×) gives three missed beats before we call a dispatch dead.
HEARTBEAT_STALE_SECONDS = 45

# Maximum age for a dispatch slot before it is force-reaped as stale regardless
# of heartbeat state.  Mirrors packs/dispatch/constants.MAX_DISPATCH_AGE_SECONDS
# (2 h = 7200 s); kept here to avoid a cross-pack import from the router.
MAX_DISPATCH_AGE_SECONDS = 7200

# Startup grace window: dirs newer than this may still be initialising.
# Mirrors packs/dispatch/constants.STARTUP_GRACE_SECONDS (60 s).
_STARTUP_GRACE_SECONDS = 60

_ORPHANS_DIR = "_orphans"
_ORPHAN_TS_FMT = "%Y%m%dT%H%M%SZ"

# Terminal / coordination fields.
FIELD_EXITCODE = "exitcode"
FIELD_HALT_MARKER = "halt_marker"
# Forensic record written next to halt_marker / timeout_marker whenever the
# supervisor halts a dispatch (#255). Single-line JSON: see
# router.dispatch.supervision._write_halt_reason for the schema.
FIELD_HALT_REASON = "halt_reason"
FIELD_TIMEOUT_MARKER = "timeout_marker"
FIELD_TRANSCRIPT = "transcript.jsonl"
FIELD_CANCEL_REASON = "cancel_reason"
# Optional human-readable one-liner set at dispatch time (issue #333).
# Rendered in launch and completion Slack lines alongside the issue number.
FIELD_SUMMARY = "summary"

# Auto-review idempotency marker. Written once when the supervision loop
# fires the auto-review @-mention so a router restart can't re-trigger it.
FIELD_AUTO_REVIEW_FIRED = ".auto_review_fired"

# Every known field, for `read_state`. Listed explicitly so we don't pick
# up unrelated files (a future feature could drop scratch files in the
# dispatch dir without polluting the state dict).
ALL_FIELDS = (
    FIELD_PID,
    FIELD_STARTED_AT,
    FIELD_RUN_STARTED_AT,
    FIELD_BUDGET,
    FIELD_CHANNEL,
    FIELD_THREAD_TS,
    FIELD_AGENT,
    FIELD_ISSUE_URL,
    FIELD_MODEL,
    FIELD_PERSONA,
    FIELD_SUMMARY,
    FIELD_LAST_EVENT,
    FIELD_LAST_TOOL,
    FIELD_COST,
    FIELD_PR_URL,
    FIELD_LAST_RATE_LIMIT_INFO,
    FIELD_EXITCODE,
    FIELD_HALT_MARKER,
    FIELD_HALT_REASON,
    FIELD_TIMEOUT_MARKER,
    FIELD_CANCEL_REASON,
)


def dispatch_root(override: str | None = None) -> Path:
    """Return the root under which dispatch dirs live.

    Resolution order: explicit ``override`` arg, then
    ``$DISPATCH_WORKSPACE_ROOT``, then ``/var/lib/dispatch``.
    """
    if override is not None:
        return Path(override)
    return Path(os.environ.get(DISPATCH_ROOT_ENV, DEFAULT_DISPATCH_ROOT))


def dispatch_dir(dispatch_id: str, *, root: str | None = None) -> Path:
    """Return the workspace dir for a single dispatch."""
    return dispatch_root(root) / dispatch_id


def ensure_dispatch_dir(dispatch_id: str, *, root: str | None = None) -> Path:
    """Create the workspace dir (mode 0700) if missing and return it."""
    d = dispatch_dir(dispatch_id, root=root)
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        # Volume may not allow chmod (e.g. some CI sandboxes); the mkdir
        # already succeeded, so silently move on.
        pass
    return d


def write_field(dispatch_id: str, field: str, value: str, *, root: str | None = None) -> Path:
    """Atomically write a single state field.

    Renames a sibling ``<field>.tmp`` over ``<field>`` so a concurrent
    :func:`read_field` never sees a partial line.
    """
    d = ensure_dispatch_dir(dispatch_id, root=root)
    final = d / field
    atomic_write_text(final, value, mkdir=False)
    return final


def read_field(dispatch_id: str, field: str, *, root: str | None = None) -> str | None:
    """Read a single state field. Returns ``None`` if missing."""
    path = dispatch_dir(dispatch_id, root=root) / field
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("read_field(%s, %s) failed", dispatch_id, field)
        return None


def read_state(dispatch_id: str, *, root: str | None = None) -> dict[str, str]:
    """Read every known field. Missing fields are simply absent from the dict."""
    state: dict[str, str] = {}
    for f in ALL_FIELDS:
        v = read_field(dispatch_id, f, root=root)
        if v is not None:
            state[f] = v
    return state


def list_dispatch_ids(*, root: str | None = None) -> list[str]:
    """Sorted list of dispatch IDs (one per subdir of the root). Empty if root missing.

    Reserved bookkeeping dirs are excluded. Dispatch IDs are always
    ``dispatch-<timestamp>-<hash>`` (see handler.py), so they never begin with
    ``.`` or ``_``. The dispatch pack's reserved top-level names
    (``.slots``, ``.queue``, ``_orphans``, ``_window`` — see
    ``packs/dispatch/constants.RESERVED_TOPLEVEL``) all use one of those two
    prefixes, so the prefix filter is a strict superset that also covers any
    future ``_``-prefixed reserved name without a cross-pack import. Without the
    ``_`` prefix the janitor's ``_orphans/`` quarantine dir leaked through here
    and made the idle check report ``active_dispatch:_orphans`` on every tick.
    """
    base = dispatch_root(root)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def find_dispatch_for_thread(
    channel: str,
    thread_ts: str,
    *,
    root: str | None = None,
) -> str | None:
    """Return the dispatch_id of any in-flight (non-terminal) dispatch for (channel, thread_ts).

    Returns None when no active dispatch matches. Used by the router to
    detect dispatch threads and route @-mentions to the agent's normal
    Slack session rather than silently dropping them (issue #173).

    Only non-terminal dispatches (no ``exitcode`` file) are considered
    in-flight. Completed dispatches are ignored so the check stays cheap
    after a thread's dispatch finishes.
    """
    for dispatch_id in list_dispatch_ids(root=root):
        if read_field(dispatch_id, FIELD_EXITCODE, root=root) is not None:
            continue
        c = read_field(dispatch_id, FIELD_CHANNEL, root=root)
        t = read_field(dispatch_id, FIELD_THREAD_TS, root=root)
        if c == channel and t == thread_ts:
            return dispatch_id
    return None


def heartbeat_alive(
    dispatch_id: str,
    *,
    root: str | None = None,
    max_age_seconds: int = HEARTBEAT_STALE_SECONDS,
    now: float | None = None,
) -> bool:
    """Is the dispatch's babysit still alive, based on heartbeat file freshness?

    Babysit touches ``<workspace>/heartbeat`` every ~15 s.  Returns
    ``True`` when the file's mtime is within ``max_age_seconds`` of now.
    Returns ``False`` when the file is absent (babysit never started or
    already removed) or stale (babysit died without writing an exitcode).

    This check is cross-namespace safe: it reads a file on the shared
    volume rather than signalling a pid that lives in a different
    container PID namespace.

    ``now`` is the reference clock as a Unix timestamp; defaults to
    ``time.time()``. Callers on a supervision tick pass their injected
    clock so a fake clock fully controls the orphan path (#259).
    """
    path = dispatch_dir(dispatch_id, root=root) / FIELD_HEARTBEAT
    try:
        mtime = path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return False
    reference = time.time() if now is None else now
    return (reference - mtime) < max_age_seconds


def is_dispatch_stale(
    dispatch_id: str,
    *,
    root: str | None = None,
    now: float | None = None,
    grace_seconds: int = _STARTUP_GRACE_SECONDS,
    max_age_seconds: int = MAX_DISPATCH_AGE_SECONDS,
) -> bool:
    """True when a dispatch has no exitcode AND is no longer alive.

    A slot is stale when it lacks an exitcode (still nominally "running") and
    either its heartbeat has gone cold or its workspace is older than
    ``max_age_seconds`` (the max-age backstop).  Slots within
    ``grace_seconds`` of creation are always considered live to avoid false
    positives during the container startup race.

    Reuses the same liveness signal as the janitor so there is exactly one
    definition of "dead dispatch" across the codebase.
    """
    now_ref = time.time() if now is None else now

    if read_field(dispatch_id, FIELD_EXITCODE, root=root) is not None:
        return False

    d = dispatch_dir(dispatch_id, root=root)
    try:
        age = now_ref - d.stat().st_mtime
    except OSError:
        return False
    if age < grace_seconds:
        return False
    if age >= max_age_seconds:
        return True
    return not heartbeat_alive(dispatch_id, root=root, now=now_ref)


def reap_stale_dispatch(
    dispatch_id: str,
    *,
    root: str | None = None,
    now: float | None = None,
) -> bool:
    """Move a stale dispatch workspace to ``_orphans/`` and return True on success.

    Idempotent: returns False silently if the workspace no longer exists.
    Uses an atomic ``os.rename`` so no partial state is visible.
    """
    base = dispatch_root(root)
    src = base / dispatch_id
    if not src.exists():
        return False
    orphans_dir = base / _ORPHANS_DIR
    try:
        orphans_dir.mkdir(exist_ok=True)
    except OSError:
        logger.exception("reap_stale_dispatch: cannot create _orphans dir")
        return False
    now_ref = time.time() if now is None else now
    ts_str = datetime.fromtimestamp(now_ref, tz=timezone.utc).strftime(_ORPHAN_TS_FMT)
    dest = orphans_dir / f"{ts_str}-{dispatch_id}"
    try:
        os.rename(src, dest)
        logger.info("reaped stale slot %s -> %s", dispatch_id, dest.name)
        return True
    except OSError:
        logger.exception("reap_stale_dispatch: rename failed for %s", dispatch_id)
        return False
