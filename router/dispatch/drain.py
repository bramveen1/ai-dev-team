"""Autodeploy drain helper — waits for all in-flight dispatches to exit.

Usage::

    python3 -m router.dispatch.drain --timeout 1800

Exit codes:
    0  drained cleanly, or --timeout 0 (skip)
    1  timeout reached (caller should proceed and SIGKILL workers)
    2  internal error (caller posts warning and proceeds)

A dispatch is *in flight* iff its dir has no ``exitcode`` file AND
``heartbeat_alive()`` returns True.  Anything else is treated as
drained/orphan.

Agent set: ``docker compose ps --services --filter status=running``
intersected with the known agent list (excludes ``router`` and
``browser-use``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from router.dispatch.state import dispatch_root, heartbeat_alive, list_dispatch_ids, read_field

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
POLL_INTERVAL_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 300  # 5 min

# Agents managed by this drain check.  ``router`` and ``browser-use`` are excluded.
KNOWN_AGENTS: frozenset[str] = frozenset({"sam", "lisa"})


def _running_agents() -> set[str]:
    """Return running agent names from docker compose, filtered to KNOWN_AGENTS."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--services", "--filter", "status=running"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        services = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return services & KNOWN_AGENTS
    except Exception:
        logger.exception("Failed to query docker compose ps")
        return set()


def _inflight_for_agent(agent: str, *, root_str: str) -> list[str]:
    """Return dispatch_ids in-flight for *agent*."""
    result = []
    for dispatch_id in list_dispatch_ids(root=root_str):
        if read_field(dispatch_id, "agent", root=root_str) != agent:
            continue
        if read_field(dispatch_id, "exitcode", root=root_str) is not None:
            continue
        if heartbeat_alive(dispatch_id, root=root_str):
            result.append(dispatch_id)
    return result


def _collect_inflight(agents: set[str], *, root_str: str) -> dict[str, list[str]]:
    """Return ``{agent: [dispatch_id, ...]}`` for all in-flight dispatches."""
    return {agent: _inflight_for_agent(agent, root_str=root_str) for agent in agents}


def _format_inflight(inflight: dict[str, list[str]]) -> str:
    parts = []
    for agent in sorted(inflight):
        for dispatch_id in inflight[agent]:
            parts.append(f"{agent}:{dispatch_id}")
    return ", ".join(parts)


def _total_inflight(inflight: dict[str, list[str]]) -> int:
    return sum(len(ids) for ids in inflight.values())


def _post_slack_sync(webhook_url: str, text: str) -> None:
    """Fire-and-forget Slack webhook post (synchronous, called via executor)."""
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.URLError:
        logger.warning("Slack webhook post failed (non-fatal)", exc_info=True)
    except Exception:
        logger.warning("Slack webhook post failed (non-fatal)", exc_info=True)


async def _post_slack(loop: asyncio.AbstractEventLoop, webhook_url: str, text: str) -> None:
    """Non-blocking wrapper around ``_post_slack_sync``."""
    if not webhook_url:
        return
    try:
        await loop.run_in_executor(None, _post_slack_sync, webhook_url, text)
    except Exception:
        logger.warning("Slack webhook executor failed (non-fatal)", exc_info=True)


async def drain(
    sha: str,
    timeout_seconds: int,
    *,
    root_path: Path | None = None,
    _running_agents_fn=None,
    _clock=None,
    _poll_interval: float = POLL_INTERVAL_SECONDS,
    _heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> int:
    """Core drain logic.  Returns exit code (0, 1, or 2).

    Parameters ``_clock``, ``_poll_interval``, and ``_heartbeat_interval`` are
    injectable for unit tests so timing can be controlled without real sleeps.
    """
    if timeout_seconds == 0:
        logger.info("--timeout 0: drain skipped")
        return 0

    clock = _clock if _clock is not None else time.monotonic
    root_str = str(root_path or dispatch_root())
    webhook_url = os.environ.get(SLACK_WEBHOOK_ENV, "")
    loop = asyncio.get_running_loop()

    get_agents = _running_agents_fn if _running_agents_fn is not None else _running_agents

    try:
        agents = get_agents()
        inflight = _collect_inflight(agents, root_str=root_str)
        n_total = _total_inflight(inflight)

        if n_total == 0:
            logger.info("No in-flight dispatches; drain complete immediately")
            return 0

        n_agents = sum(1 for ids in inflight.values() if ids)
        summary = _format_inflight(inflight)
        start_msg = (
            f":hourglass_flowing_sand: deploy {sha} queued behind {n_total} in-flight "
            f"dispatches across {n_agents} agents: {summary}"
        )
        logger.info(start_msg)
        await _post_slack(loop, webhook_url, start_msg)

        start = clock()
        last_heartbeat = start
        timeout_minutes = timeout_seconds // 60

        while True:
            elapsed = clock() - start
            if elapsed >= timeout_seconds:
                inflight = _collect_inflight(agents, root_str=root_str)
                n_remaining = _total_inflight(inflight)
                summary = _format_inflight(inflight)
                timeout_msg = (
                    f":warning: drain timeout after {timeout_minutes}m — "
                    f"forcing restart, {n_remaining} dispatches will be SIGKILL'd: {summary}"
                )
                logger.warning(timeout_msg)
                await _post_slack(loop, webhook_url, timeout_msg)
                return 1

            await asyncio.sleep(_poll_interval)

            elapsed = clock() - start
            inflight = _collect_inflight(agents, root_str=root_str)
            n_remaining = _total_inflight(inflight)

            if n_remaining == 0:
                logger.info("All dispatches drained after %.0fs", elapsed)
                return 0

            now = clock()
            if now - last_heartbeat >= _heartbeat_interval:
                last_heartbeat = now
                summary = _format_inflight(inflight)
                elapsed_minutes = int(elapsed) // 60
                hb_msg = (
                    f":hourglass: still draining ({elapsed_minutes}m / {timeout_minutes}m) — "
                    f"{n_remaining} alive: {summary}"
                )
                logger.info(hb_msg)
                await _post_slack(loop, webhook_url, hb_msg)

    except Exception:
        logger.exception("drain helper internal error")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for in-flight dispatches to exit before deploy.")
    parser.add_argument("--timeout", type=int, default=1800, help="Max seconds to wait (0 = skip)")
    parser.add_argument("--sha", default="unknown", help="New deploy SHA (for Slack messages)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s drain: %(levelname)s %(message)s")

    return asyncio.run(drain(args.sha, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
