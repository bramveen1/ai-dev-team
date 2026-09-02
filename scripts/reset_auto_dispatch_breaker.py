"""Operator action: clear the auto-dispatch circuit breaker (#868).

Run this once the signed-out Claude CLI container has been re-``/login``ed —
the breaker holds the bug loop and the epic-orchestrator loop paused
(no new docker-execs) until this is run. Logs the clear via the router's
own logger, and prints a summary either way (already-clear is a no-op, not
an error, so the script is safe to run speculatively).

Usage:

    .venv/bin/python scripts/reset_auto_dispatch_breaker.py
    .venv/bin/python scripts/reset_auto_dispatch_breaker.py --counter-path /path/to/_auto_dispatch_counters.json
"""

from __future__ import annotations

import argparse
import logging

from router.auto_dispatch.circuit_breaker import _breaker_path, clear
from router.auto_dispatch.config import DEFAULT_COUNTER_PATH

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counter-path",
        default=DEFAULT_COUNTER_PATH,
        help="Counter file the breaker sidecar is resolved next to (default: %(default)s)",
    )
    parser.add_argument("--cleared-by", default="operator", help="Recorded in the breaker state for audit")
    args = parser.parse_args()

    path = _breaker_path({"counter_path": args.counter_path})
    was_tripped = clear(path, cleared_by=args.cleared_by)
    if was_tripped:
        print(f"circuit breaker cleared ({path})")
    else:
        print(f"circuit breaker was not tripped; nothing to clear ({path})")


if __name__ == "__main__":
    main()
