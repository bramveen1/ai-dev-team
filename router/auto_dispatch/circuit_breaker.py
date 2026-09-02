"""Circuit breaker for a signed-out Claude CLI credential (#868).

``"Not logged in"`` / ``"Please run /login"`` in a worker's docker-exec
output is a **global, container-wide** condition — every subsequent
dispatch against the same container fails identically. Left unrecognized it
was silently swallowed as a generic exit-1 worker failure, so the bug loop
and the epic-orchestrator loop just kept re-firing on doomed candidates,
burning the shared ``rate_per_hour`` cap on retries that could never
succeed. Same spirit as the auth-mismatch-fail-loudly rule (#779).

The breaker trips at the single real dispatch entry point
(``_dispatch_worker`` in ``worker.py``, shared by both loops) the moment
the signed-out marker is seen in raw docker-exec output — checked before
JSON parsing, so it fires whether the handler crashed before emitting JSON
or returned a structured failure whose ``reason``/``detail`` embeds the
same text. Once tripped, every subsequent ``_dispatch_worker`` call (same
tick or a later one) raises immediately without touching docker — no
further doomed re-fires, no further rate-cap consumption — until an
operator clears it explicitly (``clear``), e.g. via
``scripts/reset_auto_dispatch_breaker.py`` once the container is
re-``/login``ed.

State is a small JSON sidecar next to the auto-dispatch counter file (same
atomic-write pattern as the other trackers in ``state.py``), so the breaker
holds across router restarts and is shared between the bug loop and the
epic loop exactly like the daily/hourly counters already are.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from router.auto_dispatch.state import _read_json, _sidecar_path, _write_json

logger = logging.getLogger(__name__)

# Case-insensitive: covers both the raw CLI status line ("Not logged in ·
# Please run /login") and either half of it appearing alone.
_SIGNED_OUT_RE = re.compile(r"not logged in|please run /login", re.IGNORECASE)

SLACK_TRIP_MESSAGE = "⛔ auto-dispatch paused: sam container signed out of Claude — re-/login required"


class SignedOutError(RuntimeError):
    """Raised by ``_dispatch_worker`` when docker-exec output shows a signed-out CLI."""


class CircuitBreakerOpenError(RuntimeError):
    """Raised by ``_dispatch_worker`` when the breaker is already tripped.

    Distinct from :class:`SignedOutError` so callers know this is a
    *repeat* doomed attempt — the loud Slack notice already went out for
    the original trip, so callers suppress rather than re-post.
    """


def looks_signed_out(text: str) -> bool:
    """Return True if *text* contains a signed-out marker."""
    return bool(text) and _SIGNED_OUT_RE.search(text) is not None


def _breaker_path(payload: dict) -> str:
    return _sidecar_path(payload, "circuit_breaker_path", "_auto_dispatch_circuit_breaker.json")


def is_tripped(path: str) -> dict[str, Any] | None:
    """Return the breaker state dict if tripped, else ``None``."""
    data = _read_json(path)
    return data if data.get("tripped") else None


def trip(path: str, *, reason: str, now_ts: float) -> bool:
    """Trip the breaker if it isn't already tripped.

    Returns True the first time (caller should surface the one-shot Slack
    notice); False on a redundant call (already tripped — stay silent).
    """
    if is_tripped(path) is not None:
        return False
    _write_json(
        path,
        {"tripped": True, "reason": reason, "tripped_ts": now_ts},
        label="circuit breaker",
    )
    logger.error("auto_dispatch: circuit breaker tripped — %s", reason)
    return True


def clear(path: str, *, cleared_by: str = "operator") -> bool:
    """Clear the breaker. Returns True if it had been tripped (and logs the clear)."""
    was_tripped = is_tripped(path) is not None
    _write_json(path, {"tripped": False, "cleared_by": cleared_by}, label="circuit breaker")
    if was_tripped:
        logger.info("auto_dispatch: circuit breaker cleared by %s", cleared_by)
    return was_tripped
