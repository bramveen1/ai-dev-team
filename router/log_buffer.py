"""In-memory ring buffer for router log lines, with secret redaction.

A :class:`LogBuffer` logging handler is installed once at router startup via
:func:`install`. The ``/logs`` HTTP endpoint (see :mod:`router.healthz`) then
serves the buffered lines to agents that hold the ``ops-diag`` pack.

Redaction
---------
Several patterns are scrubbed before a line is stored *or* returned:

- PAT-bearing URLs (``https://x-access-token:ghp_...@github.com``)
- ``ghp_`` / ``gho_`` / ``ghs_`` GitHub tokens embedded anywhere
- ``Bearer <token>`` and ``token <tok>`` authorization headers
- UUID-shaped draft IDs embedded in paths (best-effort; 8-4-4-4-12 form)
- ``user_id=<value>`` query parameters

Redaction is intentionally conservative: if a pattern might hit a secret, it
hits it; false positives on non-sensitive data are acceptable.
"""

from __future__ import annotations

import collections
import logging
import re
import threading

# Maximum number of lines kept in memory (ring-buffer cap).
DEFAULT_MAX_LINES = 2000

# Hard limits the HTTP endpoint enforces on agent requests.
MAX_TAIL_LINES = 500
MAX_BYTES = 200_000
DEFAULT_TAIL_LINES = 200
DEFAULT_MAX_BYTES = 50_000

# --- Redaction patterns ---------------------------------------------------

# Each entry is (pattern, replacement).  Compiled once at import time.
_REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    # PAT-bearing clone URLs: https://x-access-token:ghp_XXX@github.com
    (
        re.compile(r"https?://[^:@\s]+:[^@\s]+@", re.IGNORECASE),
        "https://[REDACTED]@",
    ),
    # Bare GitHub PATs / OAuth tokens (ghp_, gho_, ghs_, github_pat_)
    (
        re.compile(r"\b(ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]{10,}", re.IGNORECASE),
        r"[REDACTED-TOKEN]",
    ),
    # Generic Bearer / token Authorization header values
    (
        re.compile(r"\b(Bearer|token)\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
        r"\1 [REDACTED]",
    ),
    # user_id= query-param values (numeric or alphanumeric)
    (
        re.compile(r"(user_id=)[A-Za-z0-9_\-]{1,64}", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    # Slack bot/app tokens (xoxb-, xapp-)
    (
        re.compile(r"\b(xoxb-|xapp-)[A-Za-z0-9\-]{4,}", re.IGNORECASE),
        r"[REDACTED-TOKEN]",
    ),
]


def redact(text: str) -> str:
    """Apply all redaction rules to *text* and return the sanitised string."""
    for pattern, replacement in _REDACT_RULES:
        text = pattern.sub(replacement, text)
    return text


# --- Ring buffer ----------------------------------------------------------


class LogBuffer(logging.Handler):
    """Thread-safe logging handler that stores formatted lines in a deque."""

    def __init__(self, maxlines: int = DEFAULT_MAX_LINES) -> None:
        super().__init__()
        self._lines: collections.deque[str] = collections.deque(maxlen=maxlines)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            line = record.getMessage()
        sanitised = redact(line)
        with self._lock:
            self._lines.append(sanitised)

    def get_recent(
        self,
        tail: int = DEFAULT_TAIL_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> list[str]:
        """Return up to *tail* most-recent lines, respecting *max_bytes*.

        Lines are returned oldest-first (chronological order).  If the total
        byte count would exceed *max_bytes* before *tail* lines are consumed,
        the oldest lines are dropped until it fits.
        """
        tail = min(tail, MAX_TAIL_LINES)
        max_bytes = min(max_bytes, MAX_BYTES)

        with self._lock:
            recent = list(self._lines)[-tail:]

        # Trim from the front until the total fits within max_bytes.
        total = sum(len(ln) + 1 for ln in recent)  # +1 for newline
        while recent and total > max_bytes:
            total -= len(recent[0]) + 1
            recent.pop(0)

        return recent


# Module-level singleton installed by :func:`install`.
_buffer: LogBuffer | None = None


def install(maxlines: int = DEFAULT_MAX_LINES) -> LogBuffer:
    """Create a :class:`LogBuffer`, attach it to the root logger, and return it.

    Idempotent: a second call replaces the old handler (useful in tests that
    need a fresh buffer without restarting the process).
    """
    global _buffer

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    buf = LogBuffer(maxlines=maxlines)
    buf.setFormatter(fmt)

    root = logging.getLogger()
    # Remove any previous LogBuffer instance to stay idempotent.
    root.handlers = [h for h in root.handlers if not isinstance(h, LogBuffer)]
    root.addHandler(buf)

    _buffer = buf
    return buf


def get_buffer() -> LogBuffer | None:
    """Return the installed :class:`LogBuffer`, or ``None`` if not installed."""
    return _buffer
