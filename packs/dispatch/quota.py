"""Quota telemetry — per-window cost rollup, 80% warning, soft-lock.

Implements D-5 / #158: rolling-window cost accounting with an 80%
threshold warning and a soft-lock sentinel that blocks new dispatches
until the window expires.

All state lives under the workspace root as plain hidden files:
- ``.quota_locked``        — ISO timestamp of when the lock was set;
                             auto-clears when locked_at + window_hours ≤ now.
- ``.warning_sent_<unix>`` — sentinel touched after each per-window warning;
                             the numeric suffix is the UTC-aligned window start
                             so it ages out naturally when the window rolls.

Public API is designed for injection: callers pass ``now`` and ``root``
so tests can run without touching the real clock or filesystem.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("dispatch.quota")

DEFAULT_THRESHOLD_USD = 50.0
DEFAULT_WINDOW_HOURS = 5.0

QUOTA_LOCKED_FILE = ".quota_locked"
WARNING_SENT_PREFIX = ".warning_sent_"


def load_config(path: str | Path) -> dict[str, Any]:
    """Read ``dispatch.yaml`` at *path*. Returns safe defaults when file or keys missing."""
    defaults: dict[str, Any] = {
        "threshold_usd": DEFAULT_THRESHOLD_USD,
        "window_hours": DEFAULT_WINDOW_HOURS,
    }
    try:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}
        quota = data.get("quota") or {}
        return {
            "threshold_usd": float(quota.get("threshold_usd", DEFAULT_THRESHOLD_USD)),
            "window_hours": float(quota.get("window_hours", DEFAULT_WINDOW_HOURS)),
        }
    except Exception:
        return defaults


def _is_dispatch_dir(name: str) -> bool:
    """True for dirs that represent real dispatches vs. pool/quota infrastructure."""
    return not name.startswith(".") and not name.startswith("_")


def window_state(
    root: Path,
    now: datetime,
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> tuple[float, int, datetime | None]:
    """Scan dispatch dirs within the rolling window; return ``(cost_usd, count, oldest_started_at)``.

    Each qualifying dispatch contributes its latest ``cost`` file value
    (updated by the babysit on every event; final value remains after exit).
    Dirs with names starting with ``.`` or ``_`` (e.g. ``.slots``,
    ``.queue``, ``_orphans``) are skipped.
    """
    if not root.exists():
        return 0.0, 0, None

    cutoff = now.timestamp() - window_hours * 3600
    total_cost = 0.0
    count = 0
    oldest: datetime | None = None

    for entry in root.iterdir():
        if not entry.is_dir() or not _is_dispatch_dir(entry.name):
            continue

        try:
            sa_str = (entry / "started_at").read_text().strip()
            started_at = datetime.fromisoformat(sa_str)
        except (FileNotFoundError, OSError, ValueError):
            continue

        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        if started_at.timestamp() < cutoff:
            continue

        count += 1
        if oldest is None or started_at < oldest:
            oldest = started_at

        try:
            cost_str = (entry / "cost").read_text().strip()
            total_cost += float(cost_str)
        except (FileNotFoundError, OSError, ValueError):
            pass  # not yet written or crashed before any cost event

    return total_cost, count, oldest


def is_locked(
    root: Path,
    now: datetime,
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> tuple[bool, str | None]:
    """Check the soft-lock sentinel.

    Returns ``(locked, retry_after_iso)``. Auto-clears when the window
    has elapsed since the lock was set — returns ``(False, None)`` without
    modifying the file (it ages out naturally when the window rolls).
    """
    lock_path = root / QUOTA_LOCKED_FILE
    try:
        locked_at_str = lock_path.read_text().strip()
        locked_at = datetime.fromisoformat(locked_at_str)
    except (FileNotFoundError, OSError, ValueError):
        return False, None

    if locked_at.tzinfo is None:
        locked_at = locked_at.replace(tzinfo=timezone.utc)

    unlock_ts = locked_at.timestamp() + window_hours * 3600
    if now.timestamp() >= unlock_ts:
        return False, None

    retry_after = datetime.fromtimestamp(unlock_ts, tz=timezone.utc).isoformat()
    return True, retry_after


def mark_locked(root: Path, now: datetime) -> None:
    """Atomically write the soft-lock sentinel with *now* as the lock timestamp."""
    lock_path = root / QUOTA_LOCKED_FILE
    tmp_path = root / f".{QUOTA_LOCKED_FILE}.tmp"
    try:
        tmp_path.write_text(now.isoformat())
        os.replace(tmp_path, lock_path)
    except OSError:
        logger.exception("quota.mark_locked: failed to write %s", lock_path)


def window_start_unix(now: datetime, window_hours: float) -> int:
    """Return the UTC-aligned window start as a Unix timestamp (integer seconds)."""
    window_secs = int(window_hours * 3600)
    return int(now.timestamp()) // window_secs * window_secs


def maybe_post_warning(
    root: Path,
    now: datetime,
    slack_post_fn: Callable[..., Any],
    channel: str,
    thread_ts: str,
    *,
    threshold_usd: float = DEFAULT_THRESHOLD_USD,
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> bool:
    """Post a Slack heads-up when window cost exceeds 80% of the threshold.

    Idempotent per window: uses a ``.warning_sent_<unix_window_start>``
    sentinel so exactly one warning fires per window regardless of how
    many dispatches reach terminal state in that window.

    ``slack_post_fn`` is called as ``slack_post_fn(channel, thread_ts, text)``.
    Returns True when the warning was posted on this call.
    """
    window_cost, _, _ = window_state(root, now, window_hours=window_hours)
    if window_cost < 0.8 * threshold_usd:
        return False

    sentinel = root / f"{WARNING_SENT_PREFIX}{window_start_unix(now, window_hours)}"
    if sentinel.exists():
        return False

    text = (
        f":warning: Quota heads-up: ${window_cost:.2f} spent this window "
        f"({window_cost / threshold_usd * 100:.0f}% of ${threshold_usd:.0f} limit). "
        f"Soft-lock engages at 100%."
    )
    try:
        slack_post_fn(channel, thread_ts, text)
    except Exception:
        logger.exception("quota.maybe_post_warning: Slack post failed")
        return False

    try:
        sentinel.touch()
    except OSError:
        logger.warning("quota.maybe_post_warning: could not write sentinel %s", sentinel)

    return True


def log_window_oneliner(
    root: Path,
    now: datetime,
    log_fn: Callable[..., Any] | None = None,
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> None:
    """Log one line summarising the current window cost."""
    _log = log_fn if log_fn is not None else logger.info
    cost, count, oldest = window_state(root, now, window_hours=window_hours)
    if oldest is not None and oldest.tzinfo is not None:
        elapsed_h = (now - oldest).total_seconds() / 3600
    elif oldest is not None:
        elapsed_h = (now.replace(tzinfo=None) - oldest.replace(tzinfo=None)).total_seconds() / 3600
    else:
        elapsed_h = 0.0
    _log("window_cost: $%.2f (%d dispatches, %.1fh into window)", cost, count, elapsed_h)
