"""Babysit subprocess for an in-flight dispatch.

Spawned detached by :mod:`packs.dispatch.handler`'s ``dispatch_issue``
verb. The babysit owns the actual ``claude -p`` child process, parses
its stream-json stdout line-by-line, and updates state files under
``/var/lib/dispatch/<dispatch_id>/`` per the contract documented in
:mod:`router.dispatch.state`.

Per the supervision design in #163, the babysit has exactly one job —
keep the state files up to date while it has the subprocess open. It
does *not* post to Slack. The router-side polling supervisor reads the
state files and handles all user-facing messaging, which means a buggy
babysit can no longer make a successful dispatch look like a failure
to the launching agent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Babysit runs inside the agent container, where router/ is not
# necessarily importable. Re-declare the small subset of state-file
# constants we need; the router-side :mod:`router.dispatch.state`
# module is the source of truth and these MUST stay aligned.
DISPATCH_ROOT_ENV = "DISPATCH_WORKSPACE_ROOT"
DEFAULT_DISPATCH_ROOT = "/var/lib/dispatch"

FIELD_PID = "pid"
FIELD_HEARTBEAT = "heartbeat"
FIELD_LAST_EVENT = "last_event"
FIELD_LAST_TOOL = "last_tool"
FIELD_COST = "cost"
FIELD_EXITCODE = "exitcode"
FIELD_PR_URL = "pr_url"
TRANSCRIPT_FILE = "transcript.jsonl"

# Touch heartbeat this often. Router supervision uses 3× this as the
# stale threshold (90 s) so three consecutive missed touches = orphan.
HEARTBEAT_INTERVAL = 30  # seconds

# D-3: Slot pool directory name — must stay in sync with handler.POOL_SLOTS_DIR_NAME.
POOL_SLOTS_DIR_NAME = ".slots"

logger = logging.getLogger("dispatch.babysit")


def _root() -> Path:
    return Path(os.environ.get(DISPATCH_ROOT_ENV, DEFAULT_DISPATCH_ROOT))


def _release_slot(slot_idx: int) -> None:
    """Release a pool slot by removing its lock file. Idempotent.

    Called from ``run()``'s ``finally`` block so a crashed or SIGTERMed
    babysit never leaves its slot permanently occupied. A slot_idx of -1
    means no slot was acquired (e.g. exec_override tests); silently skip.
    """
    if slot_idx < 0:
        return
    slot_path = _root() / POOL_SLOTS_DIR_NAME / f"slot-{slot_idx}"
    try:
        slot_path.unlink()
    except (FileNotFoundError, OSError):
        pass  # already released (inline mode: handler released first) — idempotent


def _write_field(dispatch_id: str, field: str, value: str) -> None:
    """Atomically write a state file. Mirrors router.dispatch.state.write_field."""
    d = _root() / dispatch_id
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{field}.tmp"
    final = d / field
    tmp.write_text(value)
    os.replace(tmp, final)


def _touch_heartbeat(dispatch_id: str) -> None:
    """Update the heartbeat file mtime. Creates the file if absent."""
    path = _root() / dispatch_id / FIELD_HEARTBEAT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _heartbeat_loop(
    dispatch_id: str,
    stop_event: threading.Event,
    *,
    interval: float = HEARTBEAT_INTERVAL,
) -> None:
    """Background thread: touch heartbeat every *interval* seconds until stopped."""
    while not stop_event.wait(interval):
        try:
            _touch_heartbeat(dispatch_id)
        except Exception:
            pass


def _extract_event_fields(event: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Pull ``(event_type, tool_name, cost, pr_url)`` from a stream-json event.

    The ``claude -p --output-format stream-json`` envelope shape is
    documented but evolves over CLI versions, so this function is
    deliberately tolerant: each field is best-effort and ``None`` when
    not present in the current event.
    """
    event_type = event.get("type") or event.get("event_type")

    # Tool name lives at different nesting depths depending on the
    # event kind — top-level for simple events, under ``message`` for
    # assistant tool_use events.
    tool = event.get("tool_name") or event.get("name")
    if not tool:
        msg = event.get("message") or {}
        if isinstance(msg, dict):
            tool = msg.get("tool_name") or (msg.get("tool_use") or {}).get("name")

    cost = event.get("total_cost_usd")
    if cost is None:
        cost = event.get("cost_usd")
    cost_str = f"{cost}" if cost is not None else None

    # The dispatched session typically prints the PR URL on the
    # ``result`` event for a successful PR-opening flow. Sniff for any
    # github.com/.../pull/... pattern in the result text.
    pr_url: str | None = None
    result_text = event.get("result")
    if isinstance(result_text, str) and "github.com" in result_text and "/pull/" in result_text:
        for token in result_text.split():
            if "github.com" in token and "/pull/" in token:
                pr_url = token.rstrip(".,;)\"'")
                break

    return (event_type, tool, cost_str, pr_url)


def _watch(proc: subprocess.Popen, dispatch_id: str) -> None:
    """Read JSON events line-by-line, append to transcript, refresh state files."""
    transcript_path = _root() / dispatch_id / TRANSCRIPT_FILE
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    assert proc.stdout is not None  # we wired Popen with stdout=PIPE

    with open(transcript_path, "a", buffering=1) as transcript:
        for raw in proc.stdout:
            transcript.write(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type, tool, cost_str, pr_url = _extract_event_fields(event)
            if event_type:
                _write_field(dispatch_id, FIELD_LAST_EVENT, event_type)
            if tool:
                _write_field(dispatch_id, FIELD_LAST_TOOL, tool)
            if cost_str:
                _write_field(dispatch_id, FIELD_COST, cost_str)
            if pr_url:
                _write_field(dispatch_id, FIELD_PR_URL, pr_url)


def run(
    *,
    dispatch_id: str,
    cmd: list[str],
    cwd: str | None = None,
    slot_idx: int = -1,
    popen: Any = subprocess.Popen,
) -> int:
    """Run the dispatch's child process, watch it, write the terminal exitcode.

    Returns the child's exit code (or ``-1`` on spawn failure). Exits
    the parent process cleanly only after writing the ``exitcode`` file
    so a router-side supervisor that races us still sees the terminal
    state on its next tick.

    ``slot_idx`` is the D-3 pool slot index to release in the ``finally``
    block. Pass ``-1`` (default) if no slot was acquired (tests / exec_override).
    """
    # Re-record our own pid as the dispatch's pid; the handler wrote the
    # babysit's pid pre-spawn for the supervisor's benefit, but if the
    # handler crashed between the Popen call and the pid write, this
    # ensures the file matches the live process. Idempotent.
    _write_field(dispatch_id, FIELD_PID, str(os.getpid()))
    # Touch heartbeat immediately so the router supervision sees us as
    # alive from the very first tick, before any event is produced.
    _touch_heartbeat(dispatch_id)

    try:
        proc = popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=cwd,
            bufsize=1,
        )
    except (OSError, ValueError):
        logger.exception("Babysit could not spawn cmd=%s", cmd)
        _write_field(dispatch_id, FIELD_EXITCODE, "-1")
        return -1

    # Keep the heartbeat fresh while the child is running. The thread is
    # a daemon so it cannot outlive the process, but we also stop it
    # explicitly in the finally block so tests and short runs don't leave
    # a live thread after run() returns.
    _stop_heartbeat = threading.Event()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(dispatch_id, _stop_heartbeat),
        daemon=True,
        name=f"heartbeat-{dispatch_id}",
    )
    _heartbeat_thread.start()

    exit_code = -1
    try:
        try:
            _watch(proc, dispatch_id)
        except Exception:
            logger.exception("Babysit watch loop crashed; will still wait for child to exit")
        finally:
            # Explicitly close the stdout pipe so the FD doesn't leak —
            # the pytest harness treats unraisable ResourceWarnings as
            # errors, and a `with` clause on Popen isn't portable when
            # callers inject a custom popen factory in tests.
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        proc.wait()
        exit_code = proc.returncode if proc.returncode is not None else -1
    finally:
        _stop_heartbeat.set()
        _write_field(dispatch_id, FIELD_EXITCODE, str(exit_code))
        # D-3: Return the slot to the pool so the next queued dispatch can
        # proceed. This fires even on SIGTERM (Python delivers it as
        # SystemExit, which runs finally). Idempotent — safe if the handler
        # already released in inline mode.
        _release_slot(slot_idx)
    return exit_code


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dispatch.babysit", description=__doc__.splitlines()[0])
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--cwd", default=None)
    # D-3: slot index to release in finally (-1 = no slot acquired).
    parser.add_argument("--slot-idx", type=int, default=-1)
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run (precede with --)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("dispatch.babysit: refusing to run with empty cmd", file=sys.stderr)
        _write_field(args.dispatch_id, FIELD_EXITCODE, "-1")
        return 2
    started = time.time()
    rc = run(dispatch_id=args.dispatch_id, cmd=cmd, cwd=args.cwd, slot_idx=args.slot_idx)
    logger.info("Babysit exiting rc=%d after %.1fs", rc, time.time() - started)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
