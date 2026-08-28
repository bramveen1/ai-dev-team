"""Babysit subprocess for an in-flight dispatch.

Spawned detached by :mod:`packs.dispatch.handler`'s ``dispatch_issue``
verb. The babysit owns the actual ``claude -p`` child process, parses
its stream-json stdout line-by-line, and updates state files under
``/var/lib/dispatch/<dispatch_id>/`` per the contract documented in
:mod:`router.dispatch.state`.

Per the supervision design in #163, the babysit's primary job is to
keep the state files up to date while it has the subprocess open. It
also fires the D-5 quota 80% warning directly (mid-window, not just at
terminal state) so the warning fires as soon as cost crosses the threshold
rather than waiting for the router-side supervision tick.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib import request as _urlrequest
from urllib.error import URLError as _URLError

from constants import HEARTBEAT_INTERVAL, POOL_SLOTS_DIR_NAME
from slots import release_slot as _slots_release_slot
from slots import release_slot_for_dispatch as _slots_release_slot_for_dispatch

# Reuse the SIGTERM→SIGKILL grace period from the co-located handler so the
# escalation delay stays consistent. Falls back to 5s if handler.py is absent
# during a partial upgrade.
try:
    from handler import SIGTERM_GRACE_SECONDS as _SIGTERM_GRACE_SECONDS
except ImportError:
    _SIGTERM_GRACE_SECONDS = 5.0

# D-5: Quota module — co-located, import best-effort so a missing quota.py
# during a zero-downtime upgrade doesn't crash the babysit.
try:
    from datetime import datetime
    from datetime import timezone as _timezone

    import quota as _quota_mod

    _QUOTA_AVAILABLE = True
except ImportError:
    _quota_mod = None  # type: ignore[assignment]
    _QUOTA_AVAILABLE = False

# Babysit runs inside the agent container, where router/ is not
# necessarily importable. Re-declare the small subset of state-file
# constants we need; the router-side :mod:`router.dispatch.state`
# module is the source of truth and these MUST stay aligned.
DISPATCH_ROOT_ENV = "DISPATCH_WORKSPACE_ROOT"
DEFAULT_DISPATCH_ROOT = "/var/lib/dispatch"

WORKERS_BOT_TOKEN_ENV = "WORKERS_BOT_TOKEN"
# Kept for reference / healthz compatibility; no longer used for posting.
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

# Two dirs up from pack dir: /config/ in production, project root in dev.
_QUOTA_CONFIG_PATH = Path(__file__).parent.parent.parent / "dispatch.yaml"

FIELD_PID = "pid"
FIELD_HEARTBEAT = "heartbeat"
FIELD_LAST_EVENT = "last_event"
FIELD_LAST_TOOL = "last_tool"
FIELD_COST = "cost"
FIELD_EXITCODE = "exitcode"
FIELD_PR_URL = "pr_url"
FIELD_HALT_MARKER = "halt_marker"
FIELD_TIMEOUT_MARKER = "timeout_marker"
FIELD_CANCEL_REASON = "cancel_reason"
FIELD_LAST_RATE_LIMIT_INFO = "last_rate_limit_info"
TRANSCRIPT_FILE = "transcript.jsonl"

# Mirrors router.dispatch.supervision's cancel_reason values for each marker
# kind, so downstream consumers (Slack terminal summary, ops-diag) see a
# consistent label regardless of which process — babysit (this poll loop) or
# the router-side supervisor tick — wins the race to write the file.
_MARKER_CANCEL_REASON = {
    FIELD_HALT_MARKER: "stuck_guard_kill",
    FIELD_TIMEOUT_MARKER: "runtime_timeout",
}

# Un-draft idempotency marker (#769). Mirrors router.dispatch.state's
# FIELD_AUTO_REVIEW_FIRED marker-file pattern so a re-delivered pr_url event
# (or a restarted babysit) can't re-invoke `gh pr ready`.
FIELD_PR_READIED_MARKER = ".pr_readied"
GH_PR_READY_TIMEOUT_SECONDS = 15

logger = logging.getLogger("dispatch.babysit")


def _root() -> Path:
    return Path(os.environ.get(DISPATCH_ROOT_ENV, DEFAULT_DISPATCH_ROOT))


def _slack_post(channel: str, thread_ts: str, text: str) -> bool:
    """Post a Slack message via WORKERS_BOT_TOKEN.

    Raises ``RuntimeError`` when ``WORKERS_BOT_TOKEN`` is absent from the
    environment and ``channel`` is non-empty — this is intentional fail-fast
    behaviour, matching :func:`handler._post_slack_message`, to prevent the
    babysit from silently posting quota warnings under the agent identity
    (see #241, #395).

    Returns True on a successful API call, False when posting is not
    applicable (empty channel) or on transient network errors.
    """
    tok = os.environ.get(WORKERS_BOT_TOKEN_ENV)
    if not tok:
        if not channel:
            logger.debug("_slack_post: skipping post — no channel")
            return False
        raise RuntimeError("WORKERS_BOT_TOKEN not set; refusing to fall back to agent token (see #241)")
    if not channel:
        logger.warning("_slack_post: skipping post — channel is empty")
        return False
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = json.dumps(payload).encode()
    req = _urlrequest.Request(
        SLACK_API_POST_MESSAGE,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
    )
    try:
        _urlrequest.urlopen(req, timeout=5)
        return True
    except (_URLError, OSError):
        return False


def _release_slot(slot_idx: int) -> None:
    """Release a pool slot by removing its lock file. Idempotent.

    A slot_idx of -1 means no slot was acquired (e.g. exec_override tests);
    silently skip.

    NOTE: index-based release is unsafe against recycled-index races —
    production run() paths use _release_slot_for_dispatch() instead; this
    function is kept for tests that call it directly. Delegates to the
    shared slots.py protocol module.
    """
    if slot_idx < 0:
        return
    try:
        _slots_release_slot(_root() / POOL_SLOTS_DIR_NAME, slot_idx)
    except OSError:
        pass  # already released — idempotent


def _release_slot_for_dispatch(dispatch_id: str) -> None:
    """Release whichever slot file holds this dispatch_id. Idempotent.

    Safe against recycled-index races (#505). Delegates to the shared
    slots.py protocol module so the release semantics cannot drift from
    handler.py's copy again.
    """
    _slots_release_slot_for_dispatch(_root() / POOL_SLOTS_DIR_NAME, dispatch_id)


def _write_field(dispatch_id: str, field: str, value: str) -> None:
    """Atomically write a state file. Mirrors router.dispatch.state.write_field."""
    d = _root() / dispatch_id
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{field}.tmp"
    final = d / field
    tmp.write_text(value)
    os.replace(tmp, final)


def _write_terminal_exitcode(dispatch_id: str, value: str) -> None:
    """Write the terminal ``exitcode`` field from ``run()``'s ``finally``, without
    racing ``dispatch_cancel`` (#376).

    ``dispatch_cancel`` writes ``cancel_reason`` then ``exitcode`` and then
    ``rmtree``s the workspace so a racing supervision tick never observes a
    terminal dir missing its cause. If this write lands after the rmtree, a
    plain mkdir-on-write would resurrect the workspace with a lone
    ``exitcode`` file and no ``cancel_reason``. And if it lands before the
    rmtree but after cancel already wrote its own (authoritative) exitcode,
    overwriting it here would silently replace cancel's 143/137 with
    whatever this process happened to compute. Guard against both: no-op if
    the workspace dir is already gone, and no-op if ``exitcode`` is already
    present.

    The ``exists()`` check and the write are not atomic, so ``rmtree`` can
    still land in between (TOCTOU) and raise ``FileNotFoundError`` out of
    ``write_text``/``os.replace``. Treat that exactly like "already gone":
    swallow it and return, rather than letting it propagate out of
    ``run()``'s ``finally`` and skip the later ``_release_slot_for_dispatch``
    call (the #374/#394 slot-leak class).
    """
    d = _root() / dispatch_id
    if not d.exists():
        return
    final = d / FIELD_EXITCODE
    if final.exists():
        return
    tmp = d / f".{FIELD_EXITCODE}.tmp"
    try:
        tmp.write_text(value)
        os.replace(tmp, final)
    except FileNotFoundError:
        return


def _maybe_undraft_pr(dispatch_id: str, pr_url: str) -> None:
    """Best-effort, once-per-dispatch ``gh pr ready <pr_url>`` (#769).

    Un-drafts the PR deterministically at capture time so the router-side
    auto-review @-mention (``_maybe_fire_auto_review``) lands on a ready PR
    instead of depending on the worker remembering to un-draft in prose.

    Idempotency is marker-file guarded rather than an extra `gh pr view`
    call — `gh pr ready` is itself a no-op on an already-ready PR, so a
    single blind attempt per dispatch is sufficient. The marker is written
    before the `gh` call so a failing/raising call still counts as "done"
    and is never retried.
    """
    dispatch_dir = _root() / dispatch_id
    marker_path = dispatch_dir / FIELD_PR_READIED_MARKER
    if marker_path.exists():
        return
    try:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        marker_path.touch()
    except OSError:
        logger.warning("babysit: failed to write pr_readied marker for dispatch=%s", dispatch_id)
    try:
        subprocess.run(
            ["gh", "pr", "ready", pr_url],
            timeout=GH_PR_READY_TIMEOUT_SECONDS,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("babysit: gh pr ready failed for dispatch=%s pr_url=%s", dispatch_id, pr_url, exc_info=True)


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


def _marker_poll_loop(
    dispatch_id: str,
    proc: "subprocess.Popen[str]",
    stop_event: threading.Event,
    *,
    interval: float = HEARTBEAT_INTERVAL,
    grace: float = _SIGTERM_GRACE_SECONDS,
) -> None:
    """Background thread: terminate proc when a halt or timeout marker appears.

    The supervisor writes ``halt_marker`` (operator /kill) or
    ``timeout_marker`` (budget exceeded) into the dispatch workspace. By
    polling from *inside* the agent container we avoid the cross-namespace
    PID signal problem: os.killpg from the router container lands on the
    wrong process (#213). Babysit detects the marker, kills its own child,
    and writes the real exitcode — no cross-namespace signal needed.

    Kill ladder (#456): signal the whole process group so grandchild tool
    subprocesses also receive the signal. Wait ``grace`` seconds, then
    escalate to SIGKILL if the child is still alive. Set ``stop_event``
    so the heartbeat file stops being touched and the supervisor's orphan
    detector can reclaim the slot if proc.wait() in the main thread hangs.

    Invariant: cause-before-exitcode — ``cancel_reason`` is written here,
    the instant the triggering marker is detected, *before* the kill is
    even sent. run()'s ``finally`` only writes ``exitcode`` after the child
    has actually exited (which requires the kill below to have already
    happened), so this ordering guarantees cancel_reason is on disk before
    exitcode within this single process — no dependency on a router-side
    supervisor tick winning a cross-process race to supply the cause.
    """
    while not stop_event.wait(interval):
        root_path = _root() / dispatch_id
        for marker in (FIELD_HALT_MARKER, FIELD_TIMEOUT_MARKER):
            if (root_path / marker).exists():
                _write_field(dispatch_id, FIELD_CANCEL_REASON, _MARKER_CANCEL_REASON[marker])
                # Signal the whole process group to reach grandchild tool
                # subprocesses. Falls back to proc.terminate() when
                # os.getpgid / os.killpg fail (e.g. process already gone,
                # or permission error in tests that don't use a new session).
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (PermissionError, ProcessLookupError, OSError):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                # Wait the grace period, then escalate to SIGKILL.
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (PermissionError, ProcessLookupError, OSError):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                # Stop the heartbeat so the supervisor's orphan detector can
                # reclaim the slot if proc.wait() in the main thread still
                # blocks after the SIGKILL.
                stop_event.set()
                return


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
    dispatch_dir = _root() / dispatch_id
    transcript_path = dispatch_dir / TRANSCRIPT_FILE
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    assert proc.stdout is not None  # we wired Popen with stdout=PIPE

    # Read Slack context from sidecar state files written by handler at launch.
    try:
        _channel = (dispatch_dir / "channel").read_text().strip()
        _thread_ts = (dispatch_dir / "thread_ts").read_text().strip()
    except (FileNotFoundError, OSError):
        _channel = _thread_ts = ""

    # Load quota config once per watch session.
    _quota_cfg: dict | None = None
    if _QUOTA_AVAILABLE and _quota_mod is not None:
        try:
            _quota_cfg = _quota_mod.load_config(_QUOTA_CONFIG_PATH)
        except Exception:
            pass

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
                if event_type == "rate_limit_event":
                    rate_limit_info = event.get("rate_limit_info")
                    if rate_limit_info is not None:
                        try:
                            _write_field(dispatch_id, FIELD_LAST_RATE_LIMIT_INFO, json.dumps(rate_limit_info))
                        except (TypeError, ValueError):
                            pass
            if tool:
                _write_field(dispatch_id, FIELD_LAST_TOOL, tool)
            if cost_str:
                _write_field(dispatch_id, FIELD_COST, cost_str)
            if pr_url:
                _write_field(dispatch_id, FIELD_PR_URL, pr_url)
                _maybe_undraft_pr(dispatch_id, pr_url)

            # D-5: Fire 80% quota warning on every cost update so it triggers
            # mid-window as soon as the rolling total crosses the threshold.
            if cost_str and _QUOTA_AVAILABLE and _quota_mod is not None and _channel:
                try:
                    _cfg = _quota_cfg or {"threshold_usd": 50.0, "window_hours": 5.0}
                    _quota_mod.maybe_post_warning(
                        _root(),
                        datetime.now(_timezone.utc),
                        _slack_post,
                        _channel,
                        _thread_ts,
                        threshold_usd=_cfg["threshold_usd"],
                        window_hours=_cfg["window_hours"],
                    )
                except Exception:
                    logger.exception("babysit: quota.maybe_post_warning failed")

            # D-5: Detect quota_exhausted on the terminal result event and
            # mark the soft-lock so the next dispatch_issue fails fast.
            if _QUOTA_AVAILABLE and _quota_mod is not None and event_type == "result" and event.get("is_error"):
                error_type = str(event.get("error_type", ""))
                result_text = str(event.get("result", "")).lower()
                if error_type == "quota_exhausted" or "quota" in result_text:
                    try:
                        _quota_mod.mark_locked(_root(), datetime.now(_timezone.utc))
                    except Exception:
                        logger.exception("babysit: quota.mark_locked failed")


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

    # exit_code is set here (rather than at the top of the watch/wait try
    # block below) so the SIGTERM handler can record 143 via `nonlocal`
    # before it unwinds through the finally block (#376) — otherwise a
    # SIGTERM arriving before proc.wait() returns leaves exit_code at its
    # stale -1 default, and the finally block writes that instead of the
    # 143 the handler-side cancel already recorded.
    exit_code = -1

    # Belt: install a SIGTERM handler that releases the slot during the
    # 5-second grace window before SIGKILL.  Python's *default* SIGTERM
    # handler terminates immediately without unwinding finally blocks, so
    # the slot would leak on every cancel.  Our handler calls sys.exit(),
    # which raises SystemExit and *does* unwind finally — the finally block
    # then calls _release_slot_for_dispatch() a second time (idempotent for
    # the same owner; safe no-op if the index was recycled — #505).
    #
    # Record the intended signal exit code (143) before unwinding: SIGTERM
    # can land while we're blocked in proc.wait()/_watch(), in which case
    # exit_code would otherwise still be its -1 initial value when the
    # finally block persists it — surfacing a clean operator cancel as a
    # generic failure instead of the SIGTERM exit code the handler ladder
    # (packs/dispatch/handler.py's dispatch_cancel) expects.
    def _sigterm_handler(signum: int, frame: object) -> None:
        nonlocal exit_code
        exit_code = 143
        _release_slot_for_dispatch(dispatch_id)
        sys.exit(143)

    signal.signal(signal.SIGTERM, _sigterm_handler)

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
            start_new_session=True,
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

    # Poll for halt/timeout markers written by the supervisor. When found,
    # terminate the child so babysit can write its own exitcode cleanly.
    # Reuses _stop_heartbeat so both threads stop together in the finally.
    _marker_thread = threading.Thread(
        target=_marker_poll_loop,
        args=(dispatch_id, proc, _stop_heartbeat),
        daemon=True,
        name=f"marker-{dispatch_id}",
    )
    _marker_thread.start()

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
        _write_terminal_exitcode(dispatch_id, str(exit_code))
        # D-3: Return the slot to the pool so the next queued dispatch can
        # proceed. This fires even on SIGTERM (Python delivers it as
        # SystemExit, which runs finally). Owner-matched so a recycled index
        # doesn't delete a different dispatch's slot lock (#505). Idempotent
        # for the same owner — safe if the handler already released.
        _release_slot_for_dispatch(dispatch_id)
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
