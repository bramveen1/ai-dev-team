"""Persisted loop state — JSON sidecars that survive router restarts.

Four small stores, all written atomically (tmp file + ``os.replace``) beside
the counter file:

* **Counters** — daily/hourly dispatch counts, so the caps hold across restarts.
* **Awaiting tracker** — issues we dispatched that still need verdict +
  labelling. This is the bridge that makes ``handle_pr_verdict`` actually get
  *called*: ``tick`` drains this set every cycle, independent of the
  new-dispatch caps.
* **Pending-approval tracker** — issues with an un-acted approval card, so the
  same card is never re-posted on every tick during the human-decision window
  (issue #566). Entries older than ``PENDING_APPROVAL_MAX_AGE_SECONDS`` are
  treated as expired.
* **Stall state** — dedupes the "nothing eligible" Slack notification.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from router.atomic_io import atomic_read_json, atomic_write_json
from router.auto_dispatch.config import DEFAULT_COUNTER_PATH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared atomic JSON sidecar primitives
# ---------------------------------------------------------------------------


def _read_json(path: str) -> dict:
    return atomic_read_json(path, default={})


def _write_json(path: str, data: dict, *, label: str, sort_keys: bool = False) -> None:
    try:
        atomic_write_json(path, data, sort_keys=sort_keys)
    except OSError as exc:
        logger.warning("auto_dispatch: failed to write %s to %s: %s", label, path, exc)


def _sidecar_path(payload: dict, key: str, filename: str) -> str:
    """Resolve a sidecar path: explicit payload override, else beside the counter file."""
    explicit = payload.get(key)
    if explicit:
        return explicit
    counter_path = payload.get("counter_path", DEFAULT_COUNTER_PATH)
    return str(Path(counter_path).with_name(filename))


# ---------------------------------------------------------------------------
# Persisted counters (daily cap + hourly rate, survive restarts)
# ---------------------------------------------------------------------------


def _read_counters(path: str) -> dict:
    return _read_json(path)


def _write_counters(path: str, data: dict) -> None:
    _write_json(path, data, label="counters")


def _today_str(now: float | None = None) -> str:
    """Return YYYY-MM-DD in UTC for the given timestamp (or now)."""
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _current_hour_str(now: float | None = None) -> str:
    """Return YYYY-MM-DDTHH in UTC for the given timestamp (or now)."""
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H")


def get_counters(counter_path: str, now: float | None = None) -> dict:
    """Return ``{daily_count, hourly_count}`` for the current window."""
    data = _read_counters(counter_path)
    today = _today_str(now)
    hour = _current_hour_str(now)
    return {
        "daily_count": data.get("daily_count", 0) if data.get("daily_date") == today else 0,
        "hourly_count": data.get("hourly_count", 0) if data.get("hourly_hour") == hour else 0,
    }


def increment_counters(counter_path: str, now: float | None = None) -> None:
    """Atomically bump daily and hourly dispatch counters."""
    data = _read_counters(counter_path)
    today = _today_str(now)
    hour = _current_hour_str(now)

    daily_count = data.get("daily_count", 0) if data.get("daily_date") == today else 0
    hourly_count = data.get("hourly_count", 0) if data.get("hourly_hour") == hour else 0

    _write_counters(
        counter_path,
        {
            "daily_date": today,
            "daily_count": daily_count + 1,
            "hourly_hour": hour,
            "hourly_count": hourly_count + 1,
        },
    )


def decrement_counters(counter_path: str, now: float | None, enqueued_ts: float | None) -> None:
    """Atomically refund daily and hourly dispatch counters for a no-op close.

    Mirrors ``increment_counters``'s atomic read-modify-write, but only refunds
    a bucket (daily / hourly, independently) whose ``enqueued_ts`` falls in the
    *same* bucket as ``now`` — a dispatch enqueued in a since-rolled hour/day
    already had its counter reset by ``get_counters``, so refunding it would
    wrongly credit the current window instead. Both counts are floored at 0.
    """
    data = _read_counters(counter_path)
    today = _today_str(now)
    hour = _current_hour_str(now)

    daily_count = data.get("daily_count", 0) if data.get("daily_date") == today else 0
    hourly_count = data.get("hourly_count", 0) if data.get("hourly_hour") == hour else 0

    if _today_str(enqueued_ts) == today:
        daily_count = max(0, daily_count - 1)
    if _current_hour_str(enqueued_ts) == hour:
        hourly_count = max(0, hourly_count - 1)

    _write_counters(
        counter_path,
        {
            "daily_date": today,
            "daily_count": daily_count,
            "hourly_hour": hour,
            "hourly_count": hourly_count,
        },
    )


# ---------------------------------------------------------------------------
# Awaiting tracker — issues dispatched, still needing verdict + labelling.
# Keyed by issue number → enqueue timestamp.
# ---------------------------------------------------------------------------


def _awaiting_path(payload: dict) -> str:
    return _sidecar_path(payload, "awaiting_path", "_auto_dispatch_awaiting.json")


def _read_awaiting(path: str) -> dict:
    return _read_json(path)


def _write_awaiting(path: str, data: dict) -> None:
    _write_json(path, data, label="awaiting set")


def _add_awaiting(path: str, issue_num: int, now_ts: float) -> None:
    data = _read_awaiting(path)
    data[str(issue_num)] = now_ts
    _write_awaiting(path, data)


def _remove_awaiting(path: str, issue_num: int) -> None:
    data = _read_awaiting(path)
    if data.pop(str(issue_num), None) is not None:
        _write_awaiting(path, data)


# ---------------------------------------------------------------------------
# Pending-approval tracker — issues with an un-acted approval card (#566).
# Keyed by issue number → card-posted timestamp.
# ---------------------------------------------------------------------------


def _pending_approval_path(payload: dict) -> str:
    return _sidecar_path(payload, "pending_approval_path", "_auto_dispatch_pending_approval.json")


def _read_pending_approval(path: str) -> dict:
    return _read_json(path)


def _write_pending_approval(path: str, data: dict) -> None:
    _write_json(path, data, label="pending-approval set")


def _add_pending_approval(path: str, issue_num: int, now_ts: float) -> None:
    data = _read_pending_approval(path)
    data[str(issue_num)] = now_ts
    _write_pending_approval(path, data)


def _remove_pending_approval(path: str, issue_num: int) -> None:
    data = _read_pending_approval(path)
    if data.pop(str(issue_num), None) is not None:
        _write_pending_approval(path, data)


# ---------------------------------------------------------------------------
# Stall state sidecar — deduplicates "nothing eligible" Slack notifications.
# ---------------------------------------------------------------------------


def _stall_state_path(payload: dict) -> str:
    return _sidecar_path(payload, "stall_state_path", "_auto_dispatch_last_stall.json")


def _read_last_stall_state(path: str) -> dict:
    return _read_json(path)


def _write_last_stall_state(path: str, state: dict) -> None:
    _write_json(path, state, label="stall state", sort_keys=True)
