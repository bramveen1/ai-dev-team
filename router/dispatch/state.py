"""Sidecar state files for in-flight dispatches.

Layout under ``/var/lib/dispatch/<dispatch_id>/``:

| File             | Writer                                | When                              |
|------------------|---------------------------------------|-----------------------------------|
| ``pid``          | handler                               | once at launch (babysit pid)      |
| ``started_at``   | handler                               | once at launch (ISO-8601)         |
| ``budget``       | handler                               | once at launch (seconds, int)     |
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
from pathlib import Path

logger = logging.getLogger(__name__)

DISPATCH_ROOT_ENV = "DISPATCH_WORKSPACE_ROOT"
DEFAULT_DISPATCH_ROOT = "/var/lib/dispatch"

# Handler-written fields (one-shot at launch).
FIELD_PID = "pid"
FIELD_STARTED_AT = "started_at"
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

# Terminal / coordination fields.
FIELD_EXITCODE = "exitcode"
FIELD_HALT_MARKER = "halt_marker"
FIELD_TRANSCRIPT = "transcript.jsonl"
FIELD_CANCEL_REASON = "cancel_reason"

# Every known field, for `read_state`. Listed explicitly so we don't pick
# up unrelated files (a future feature could drop scratch files in the
# dispatch dir without polluting the state dict).
ALL_FIELDS = (
    FIELD_PID,
    FIELD_STARTED_AT,
    FIELD_BUDGET,
    FIELD_CHANNEL,
    FIELD_THREAD_TS,
    FIELD_AGENT,
    FIELD_ISSUE_URL,
    FIELD_MODEL,
    FIELD_PERSONA,
    FIELD_LAST_EVENT,
    FIELD_LAST_TOOL,
    FIELD_COST,
    FIELD_PR_URL,
    FIELD_EXITCODE,
    FIELD_HALT_MARKER,
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
    tmp = d / f".{field}.tmp"
    tmp.write_text(value)
    os.replace(tmp, final)
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
    """Sorted list of dispatch IDs (one per subdir of the root). Empty if root missing."""
    base = dispatch_root(root)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def pid_alive(pid: int) -> bool:
    """Best-effort: is ``pid`` still running?

    Returns ``True`` on ``PermissionError`` — the process exists but
    belongs to another uid, which means we can't signal it but we
    shouldn't claim it's dead either.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
