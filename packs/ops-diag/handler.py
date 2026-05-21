"""CLI entry point for the ops-diag pack (issue #218).

The agent invokes this script from Bash:

    python /config/packs/ops-diag/handler.py <verb> [--<arg> ...]

Verbs:

- ``router_logs`` — fetch recent router log lines from the router's
  ``/logs`` HTTP endpoint (running on the Docker-internal network at
  ``http://router:8080``).  Lines have already been redacted by the
  router's ring buffer before this handler receives them.

``router_logs`` flags:

- ``--tail N``        lines to fetch (default 200, max 500)
- ``--max-bytes N``   byte cap on the router response (default 50 000)
- ``--router-url U``  override the router base URL (default: env var
                      ``ROUTER_LOGS_URL`` or ``http://router:8080``)

Exit codes:

- 0   success
- 1   usage error
- 2   network / HTTP error
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib import request as _urlrequest
from urllib.error import URLError

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NETWORK = 2

DEFAULT_ROUTER_URL = "http://router:8080"
_ROUTER_URL_ENV = "ROUTER_LOGS_URL"


def _router_base_url() -> str:
    import os

    return os.environ.get(_ROUTER_URL_ENV, DEFAULT_ROUTER_URL).rstrip("/")


def router_logs(tail: int, max_bytes: int, router_url: str) -> dict:
    url = f"{router_url}/logs?tail={tail}&max_bytes={max_bytes}"
    try:
        with _urlrequest.urlopen(url, timeout=10) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
    except URLError as exc:
        return {"status": "error", "error": str(exc), "lines": [], "line_count": 0}

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"invalid JSON from router: {exc}", "lines": [], "line_count": 0}

    return data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-diag router_logs")
    parser.add_argument("--tail", type=int, default=200, metavar="N")
    parser.add_argument("--max-bytes", type=int, default=50_000, dest="max_bytes", metavar="N")
    parser.add_argument("--router-url", default=None, dest="router_url", metavar="URL")
    return parser


def run() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "error": "no verb specified"}))
        return EXIT_USAGE

    verb = sys.argv[1]
    rest = sys.argv[2:]

    if verb == "router_logs":
        args = _build_parser().parse_args(rest)
        base = args.router_url or _router_base_url()
        result = router_logs(tail=args.tail, max_bytes=args.max_bytes, router_url=base)
        print(json.dumps(result))
        return EXIT_OK if result.get("status") in ("ok", "unavailable") else EXIT_NETWORK

    print(json.dumps({"status": "error", "error": f"unknown verb: {verb!r}"}))
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(run())
