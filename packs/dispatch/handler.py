"""CLI entry point for the dispatch pack.

The agent invokes this script from Bash:

    python /config/packs/dispatch/handler.py <verb> [--<arg> ...]

Verbs:

- ``dispatch_health``   — smoke probe.
- ``dispatch_issue``    — primary verb, spawns a headless claude session
                          and returns ``{status: "launched", dispatch_id}``
                          in <3s; supervision lives router-side (#163).
- ``dispatch_status``   — read latest event from an in-flight dispatch.
- ``dispatch_cancel``   — SIGTERM the process group, tear down workspace.

``dispatch_health`` returns four fields the operator cares about:

- ``cli_version``               — output of ``claude --version`` (str).
- ``claude_path``               — absolute path to the resolved binary.
- ``workspace_volume_writable`` — does a touch under ``/var/lib/dispatch/``
                                  succeed?
- ``sonnet_probe_ok``           — does a 30s ``claude -p`` against Sonnet
                                  round-trip cleanly?

The probe is Sonnet-pinned on purpose: health checks fire on every Sam
container start and we'd rather not burn the Opus 5h quota on liveness.

``dispatch_issue`` returns ``{status: "launched", dispatch_id, workspace,
pid}`` after writing the initial sidecar state files and forking the
babysit subprocess. It does NOT block on completion — the router-side
supervisor polls the state files and posts the terminal message back
into the original Slack thread (see :mod:`router.dispatch.supervision`).

Failures in ``dispatch_health`` are surfaced as ``false`` in the
relevant field plus a structured detail (CLI exit code, error message)
— they MUST NOT raise through to a 500. The acceptance criteria spell
that out.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("dispatch.handler")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LAUNCH_FAILED = 3

# Env-var fallbacks for Slack context, injected by the router's
# ``pack_cli_extras`` when an agent has the dispatch pack enabled. These
# let an agent dispatch itself without explicit ``--channel`` /
# ``--thread-ts`` / ``--agent`` flags; explicit flags always win.
DISPATCH_CHANNEL_ENV = "DISPATCH_CHANNEL"
DISPATCH_THREAD_TS_ENV = "DISPATCH_THREAD_TS"
DISPATCH_AGENT_ENV = "DISPATCH_AGENT"

# Workspace volume root. Sam's docker-compose entry mounts the
# ``dispatch-workspaces`` named volume here. Overridable via env for
# unit tests (which point it at a tmp dir).
WORKSPACE_ROOT_ENV = "DISPATCH_WORKSPACE_ROOT"
DEFAULT_WORKSPACE_ROOT = "/var/lib/dispatch"

# Sonnet probe configuration. Pinned to Sonnet so we never burn Opus
# quota on liveness checks (locked decision in the design doc).
SONNET_PROBE_MODEL = "sonnet"
SONNET_PROBE_PROMPT = "echo: hello"
SONNET_PROBE_TIMEOUT_S = 30.0
SONNET_PROBE_EXPECTED_TOKEN = "hello"

# Default per-dispatch wallclock budget. Beyond this the router-side
# supervisor SIGTERMs the subprocess and writes a synthetic exitcode
# (see :mod:`router.dispatch.supervision`). 30 min matches the design
# doc's ``max_runtime_seconds`` default.
DEFAULT_BUDGET_SECONDS = 30 * 60

# Kill-ladder exit codes: 128 + signal number.
EXITCODE_SIGTERM = 143  # 128 + 15
EXITCODE_SIGKILL = 137  # 128 + 9

# Seconds between SIGTERM and SIGKILL in the cancel kill ladder.
SIGTERM_GRACE_SECONDS = 5.0

# Default model for dispatch_issue. Issue #163 explicitly recommends
# pinning Sonnet as the default to avoid burning the Opus 5h window on
# routine dispatches; the operator can override with ``--model``.
DEFAULT_DISPATCH_MODEL = "sonnet"
DEFAULT_DISPATCH_PERSONA = "dev"

# Path to the babysit subprocess. Co-located with the handler so the
# pack ships as a single directory.
BABYSIT_PATH = str(Path(__file__).parent / "babysit.py")

# Strong references to detached babysit Popen objects so Python's GC
# doesn't tear them down (and fire a ResourceWarning) before the OS
# has reaped the child. In production the handler runs as a Sam-spawned
# subprocess that exits long before the babysit does — Sam itself ends
# up reaping the orphan via init — so this list is effectively only
# load-bearing for in-process callers like the smoke probe and the
# CLI test, where Popen GC inside the same Python interpreter would
# otherwise emit "subprocess still running" warnings.
_DETACHED_BABYSITS: list[Any] = []

# Supervision mode toggle from #163's rollback section. ``inline`` runs
# the babysit foreground and blocks until ``exitcode`` lands, then
# returns the terminal envelope inline — that's the pre-#163 behavior
# preserved for safety. ``poll`` detaches the babysit and relies on the
# router-side discovery + supervision loops to surface the result back
# to the Slack thread. Default is ``inline`` until the poll path proves
# itself on 10–20 real dispatches, exactly as the issue spelled out;
# operators flip to ``poll`` via the env var without a router restart.
SUPERVISION_MODE_ENV = "DISPATCH_SUPERVISION"
SUPERVISION_MODE_INLINE = "inline"
SUPERVISION_MODE_POLL = "poll"
DEFAULT_SUPERVISION_MODE = SUPERVISION_MODE_INLINE


def _supervision_mode() -> str:
    """Read the supervision mode from the env, normalized + validated.

    Unknown values fall back to ``inline`` with a log line — silently
    treating ``DISPATCH_SUPERVISION=pol`` as ``poll`` would defeat the
    point of having an explicit safety default.
    """
    raw = (os.environ.get(SUPERVISION_MODE_ENV) or DEFAULT_SUPERVISION_MODE).strip().lower()
    if raw in (SUPERVISION_MODE_INLINE, SUPERVISION_MODE_POLL):
        return raw
    logger.warning(
        "Unknown %s=%r; falling back to %s",
        SUPERVISION_MODE_ENV,
        raw,
        DEFAULT_SUPERVISION_MODE,
    )
    return DEFAULT_SUPERVISION_MODE


def _workspace_root() -> Path:
    return Path(os.environ.get(WORKSPACE_ROOT_ENV, DEFAULT_WORKSPACE_ROOT))


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _kill_pg(pgid: int, sig: int) -> bool:
    """Send ``sig`` to process group ``pgid``. Returns True on success.

    Falls back to ``os.kill`` when ``os.killpg`` raises PermissionError
    (unit tests that don't start the babysit in its own session) so
    tests can exercise this path without real process groups.
    """
    for killer in (
        lambda: os.killpg(pgid, sig),
        lambda: os.kill(pgid, sig),
    ):
        try:
            killer()
            return True
        except ProcessLookupError:
            return False  # already gone — treat as success
        except (PermissionError, OSError):
            continue
    return False


def _is_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` still exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but we can't signal it


def _resolve_claude(which: object = shutil.which) -> str | None:
    """Return the absolute path to the ``claude`` CLI, or ``None``.

    ``which`` is injectable so tests can stub it without monkey-patching
    the whole ``shutil`` module.
    """
    return which("claude")  # type: ignore[operator]


def _read_cli_version(claude_path: str | None, *, run: Any = subprocess.run) -> tuple[str | None, int | None]:
    """Return ``(version_string, exit_code)``. Both ``None`` if the CLI is missing."""
    if not claude_path:
        return None, None
    try:
        completed = run(
            [claude_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("claude --version failed: %s", e)
        return None, None
    if completed.returncode != 0:
        return None, completed.returncode
    version = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return version or None, completed.returncode


def _check_workspace_writable(root: Path) -> bool:
    """Touch ``<root>/.health`` and remove it. Any failure is ``False`` — no raise.

    The acceptance criteria are explicit: missing volume mount must not
    surface as a 500. Treat every OSError as ``writable=False`` and
    move on.
    """
    if not root.exists():
        logger.info("workspace root missing: %s", root)
        return False
    marker = root / ".health"
    try:
        marker.write_text("ok\n")
    except OSError as e:
        logger.info("workspace root not writable: %s — %s", root, e)
        return False
    try:
        marker.unlink()
    except OSError:
        # Removal failure is fine — write succeeded, mount is writable.
        # The janitor will sweep the stale marker later.
        pass
    return True


def _run_sonnet_probe(
    claude_path: str | None,
    *,
    run: Any = subprocess.run,
) -> tuple[bool, int | None, str | None]:
    """Run a 30s Sonnet round-trip. Return ``(ok, exit_code, detail)``.

    The probe shells out to::

        claude -p "echo: hello" --model sonnet \
          --output-format json --permission-mode acceptEdits

    Expects ``is_error: false`` and ``result`` containing ``"hello"`` in
    the JSON envelope. Any deviation (non-zero exit, timeout, bad JSON,
    missing result) is ``(False, exit_code, detail)`` — never raises.
    """
    if not claude_path:
        return False, None, "claude binary not on PATH"
    try:
        completed = run(
            [
                claude_path,
                "-p",
                SONNET_PROBE_PROMPT,
                "--model",
                SONNET_PROBE_MODEL,
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
            ],
            capture_output=True,
            text=True,
            timeout=SONNET_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, None, f"sonnet probe timed out after {SONNET_PROBE_TIMEOUT_S}s"
    except OSError as e:
        return False, None, f"sonnet probe failed to spawn: {e}"

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "")[-200:].strip()
        return False, completed.returncode, stderr_tail or "non-zero exit"

    raw = (completed.stdout or "").strip()
    if not raw:
        return False, completed.returncode, "empty stdout from claude -p"
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, completed.returncode, f"unparseable JSON: {e}"

    if not isinstance(envelope, dict):
        return False, completed.returncode, "JSON envelope is not an object"
    if envelope.get("is_error"):
        return False, completed.returncode, f"is_error=true: {envelope.get('result') or envelope!r}"
    result = envelope.get("result")
    if not isinstance(result, str) or SONNET_PROBE_EXPECTED_TOKEN not in result.lower():
        return False, completed.returncode, f"result did not contain {SONNET_PROBE_EXPECTED_TOKEN!r}: {result!r}"

    return True, completed.returncode, None


def dispatch_health(
    *,
    which: Any = shutil.which,
    run: Any = subprocess.run,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Return the four required health fields plus structured failure detail.

    Shape::

        {
          "cli_version": str | None,
          "claude_path": str | None,
          "workspace_volume_writable": bool,
          "sonnet_probe_ok": bool,
          "sonnet_probe_exit_code": int | None,   # only when probe ran
          "sonnet_probe_detail": str | None,      # only on failure
        }

    Never raises on the failure modes the acceptance criteria call out
    (missing volume → ``workspace_volume_writable: false``; failing
    Sonnet probe → ``sonnet_probe_ok: false`` with exit code).
    """
    claude_path = _resolve_claude(which)
    cli_version, _ = _read_cli_version(claude_path, run=run)
    root = workspace_root if workspace_root is not None else _workspace_root()
    writable = _check_workspace_writable(root)
    sonnet_ok, sonnet_exit, sonnet_detail = _run_sonnet_probe(claude_path, run=run)

    out: dict[str, Any] = {
        "cli_version": cli_version,
        "claude_path": claude_path,
        "workspace_volume_writable": writable,
        "sonnet_probe_ok": sonnet_ok,
        "sonnet_probe_exit_code": sonnet_exit,
    }
    if not sonnet_ok:
        out["sonnet_probe_detail"] = sonnet_detail
    return out


def _new_dispatch_id(now: datetime) -> str:
    """Generate a sortable, unique dispatch id.

    Format: ``dispatch-<utc-iso-compact>-<6hex>``. The timestamp prefix
    makes ``ls`` order match launch order; the random suffix avoids
    collisions when two dispatches launch in the same second.
    """
    return f"dispatch-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _atomic_write(path: Path, value: str) -> None:
    """Atomically write a single-value sidecar file.

    Mirrors :func:`router.dispatch.state.write_field` so a supervisor
    that polls during launch never sees a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(value)
    os.replace(tmp, path)


def _build_claude_command(
    *,
    issue_url: str,
    model: str,
    persona: str,
    workspace: Path,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Render the ``claude -p`` invocation the babysit will exec.

    Kept narrow on purpose — workspace setup (git clone, ``CLAUDE_CONFIG_DIR``
    copy) is handled by separate follow-up work tracked in
    ``docs/design/dispatch-pack.md``. This builds the minimal command
    that exercises the supervision contract end-to-end; the dispatched
    session is responsible for opening the PR itself.
    """
    prompt = (
        f"Work on GitHub issue {issue_url} as persona={persona}. "
        f"Read the issue, implement the requested change in this workspace, "
        f"commit on a fresh branch, push, and open a pull request."
    )
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        # ``--output-format stream-json`` requires ``--verbose`` on recent
        # Claude Code CLIs (>=2.x); without it the child exits immediately
        # with: "When using --print, --output-format=stream-json requires
        # --verbose". The babysit parser is field-tolerant
        # (``_extract_event_fields``), so the richer verbose framing is a
        # superset of what we already consume.
        "--verbose",
        # Dispatched workers run inside the agent container in an ephemeral
        # workspace (``/tmp/dispatch-<id>/``). ``--permission-mode acceptEdits``
        # only auto-allows Edit/Write — every ``Bash(gh ...)`` and
        # ``WebFetch(...)`` call from the worker would otherwise be denied
        # (see post-mortem on issue #169). The container is already the
        # sandbox: limited mounts, scoped GITHUB_TOKEN, no host docker
        # socket, no privileged caps. ``--dangerously-skip-permissions``
        # bypasses the interactive permission prompt so the dispatched
        # session can actually do its job.
        "--dangerously-skip-permissions",
        "--add-dir",
        str(workspace),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def dispatch_issue(
    *,
    issue_url: str,
    channel: str,
    thread_ts: str,
    agent: str,
    budget_seconds: int = DEFAULT_BUDGET_SECONDS,
    model: str = DEFAULT_DISPATCH_MODEL,
    persona: str = DEFAULT_DISPATCH_PERSONA,
    exec_override: list[str] | None = None,
    workspace_root: Path | None = None,
    babysit_path: str | None = None,
    popen: Any = subprocess.Popen,
    now: datetime | None = None,
    supervision_mode: str | None = None,
) -> dict[str, Any]:
    """Launch a headless dispatch.

    Behavior depends on the supervision mode (set explicitly here or
    resolved from ``$DISPATCH_SUPERVISION``, default ``inline``):

    * **inline** (default) — run the babysit foreground, wait for
      ``exitcode`` to land, return ``{status: "completed", exitcode,
      ...}`` with the full terminal envelope. This is the pre-#163
      blocking path, kept as the safety default while the poll path
      proves itself (see #163's rollback section).
    * **poll** — write the launch sidecar state, spawn the babysit
      detached (``start_new_session=True``), and return
      ``{status: "launched", dispatch_id, ...}`` in <3s. The router-side
      discovery loop (:mod:`router.dispatch.discovery`) picks the
      dispatch dir up on its next tick and registers the supervision
      system task.

    Both modes write the same sidecar state files in the same order so
    a router-side observer that races inline-mode mid-flight still sees
    a coherent prefix.

    ``exec_override`` exists for tests and the smoke probe: pass a list
    like ``["sleep", "30"]`` and the babysit will run that instead of
    the real ``claude -p`` command.
    """
    mode = (supervision_mode or _supervision_mode()).lower()
    now = now or datetime.now(timezone.utc)
    root = workspace_root if workspace_root is not None else _workspace_root()
    dispatch_id = _new_dispatch_id(now)
    workspace = root / dispatch_id
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        workspace.chmod(0o700)
    except OSError:
        # Some volume backings (notably CI sandboxes) refuse chmod.
        # The mkdir already succeeded and the subsequent file writes
        # don't need 0700 to function — just less private.
        pass

    # Initial state files (everything the supervisor needs except pid).
    # Written in the order documented in router/dispatch/state.py so a
    # supervisor poll that races us mid-write sees a coherent prefix.
    _atomic_write(workspace / "started_at", now.isoformat())
    _atomic_write(workspace / "budget", str(int(budget_seconds)))
    _atomic_write(workspace / "channel", channel)
    _atomic_write(workspace / "thread_ts", thread_ts)
    _atomic_write(workspace / "agent", agent)
    _atomic_write(workspace / "issue_url", issue_url)
    _atomic_write(workspace / "model", model)
    _atomic_write(workspace / "persona", persona)

    child_cmd = (
        list(exec_override)
        if exec_override
        else _build_claude_command(
            issue_url=issue_url,
            model=model,
            persona=persona,
            workspace=workspace,
        )
    )

    babysit = babysit_path or BABYSIT_PATH
    babysit_argv = [sys.executable, babysit, "--dispatch-id", dispatch_id, "--cwd", str(workspace), "--"] + child_cmd

    base_response = {
        "dispatch_id": dispatch_id,
        "workspace": str(workspace),
        "budget_seconds": int(budget_seconds),
        "model": model,
        "persona": persona,
        "supervision_mode": mode,
    }

    if mode == SUPERVISION_MODE_INLINE:
        return _launch_inline(
            popen=popen,
            babysit_argv=babysit_argv,
            workspace=workspace,
            base_response=base_response,
        )

    return _launch_poll(
        popen=popen,
        babysit_argv=babysit_argv,
        workspace=workspace,
        base_response=base_response,
    )


def _launch_poll(
    *,
    popen: Any,
    babysit_argv: list[str],
    workspace: Path,
    base_response: dict[str, Any],
) -> dict[str, Any]:
    """Poll-mode launch: spawn babysit detached, write pid, return ``launched``."""
    try:
        proc = popen(
            babysit_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(workspace),
        )
    except (OSError, ValueError) as e:
        # Spawn failed — record a synthetic exitcode so any supervisor
        # we've already registered sees terminal state immediately,
        # rather than waiting for the orphan-detection path.
        _atomic_write(workspace / "exitcode", "-1")
        return {"status": "launch_failed", "error": str(e), **base_response}

    _atomic_write(workspace / "pid", str(proc.pid))
    # Park the Popen handle so the GC doesn't tear it down (and emit a
    # ResourceWarning about the still-running subprocess) before the OS
    # reaper gets to the babysit. See _DETACHED_BABYSITS above for why
    # this only matters for in-process callers.
    _DETACHED_BABYSITS.append(proc)
    return {"status": "launched", "pid": proc.pid, **base_response}


def _launch_inline(
    *,
    popen: Any,
    babysit_argv: list[str],
    workspace: Path,
    base_response: dict[str, Any],
) -> dict[str, Any]:
    """Inline-mode launch: run babysit foreground, wait for ``exitcode``, return ``completed``.

    Pre-#163 blocking behavior — the agent's bash call doesn't return
    until the dispatch is done. The router-side discovery loop sees the
    dispatch dir already terminal (``exitcode`` present before the dir
    is observed) and skips it.
    """
    try:
        proc = popen(
            babysit_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(workspace),
        )
    except (OSError, ValueError) as e:
        _atomic_write(workspace / "exitcode", "-1")
        return {"status": "launch_failed", "error": str(e), **base_response}

    _atomic_write(workspace / "pid", str(proc.pid))
    # Block on completion. The babysit always writes exitcode in its
    # ``finally``, so .wait() returning means the file exists.
    returncode = proc.wait()
    exitcode_path = workspace / "exitcode"
    exitcode_str = exitcode_path.read_text().strip() if exitcode_path.exists() else str(returncode)

    pr_url_path = workspace / "pr_url"
    cost_path = workspace / "cost"
    response: dict[str, Any] = {
        "status": "completed" if exitcode_str == "0" else "failed",
        "pid": proc.pid,
        "exitcode": int(exitcode_str) if exitcode_str.lstrip("-").isdigit() else -1,
        **base_response,
    }
    if pr_url_path.exists():
        response["pr_url"] = pr_url_path.read_text().strip()
    if cost_path.exists():
        response["cost"] = cost_path.read_text().strip()
    return response


def _resolve_slack_context(
    *,
    channel: str | None,
    thread_ts: str | None,
    agent: str | None,
    environ: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """Resolve ``(channel, thread_ts, agent)`` from flags with env fallback.

    Flags win over env. Empty strings count as unset (so a stray
    ``--channel ""`` still surfaces a clear missing-variable error
    rather than passing through to write a state file with no value).
    Raises ``ValueError`` listing every missing field — never silently
    defaults to a wrong channel, per the issue's negative-path
    acceptance criterion.
    """
    env = environ if environ is not None else os.environ
    resolved_channel = channel or env.get(DISPATCH_CHANNEL_ENV) or ""
    resolved_thread_ts = thread_ts or env.get(DISPATCH_THREAD_TS_ENV) or ""
    resolved_agent = agent or env.get(DISPATCH_AGENT_ENV) or ""

    missing: list[str] = []
    if not resolved_channel:
        missing.append(f"--channel or ${DISPATCH_CHANNEL_ENV}")
    if not resolved_thread_ts:
        missing.append(f"--thread-ts or ${DISPATCH_THREAD_TS_ENV}")
    if not resolved_agent:
        missing.append(f"--agent or ${DISPATCH_AGENT_ENV}")
    if missing:
        raise ValueError("missing required Slack context: " + ", ".join(missing))

    return resolved_channel, resolved_thread_ts, resolved_agent


def _build_issue_parser() -> argparse.ArgumentParser:
    """Argument parser for the ``dispatch_issue`` verb (kept separate so
    the top-level dispatcher can hand off cleanly without losing the
    "unknown verb" behavior callers test for).

    ``--channel`` / ``--thread-ts`` / ``--agent`` are optional at parse
    time — they fall back to ``DISPATCH_CHANNEL`` / ``DISPATCH_THREAD_TS``
    / ``DISPATCH_AGENT`` env vars in :func:`_resolve_slack_context`. This
    lets an agent dispatch itself from inside its container (env injected
    by the router's pack hook) without surfacing Slack IDs into the
    prompt, while preserving host-side invocation that passes the flags.
    """
    parser = argparse.ArgumentParser(prog="dispatch.handler dispatch_issue", add_help=False)
    parser.add_argument("--issue-url", required=True)
    parser.add_argument("--channel", default=None)
    parser.add_argument("--thread-ts", default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--model", default=DEFAULT_DISPATCH_MODEL)
    parser.add_argument("--persona", default=DEFAULT_DISPATCH_PERSONA)
    parser.add_argument(
        "--supervision-mode",
        choices=[SUPERVISION_MODE_INLINE, SUPERVISION_MODE_POLL],
        default=None,
        help="Override $DISPATCH_SUPERVISION for this invocation.",
    )
    parser.add_argument(
        "--exec",
        dest="exec_override",
        nargs=argparse.REMAINDER,
        default=None,
        help="Override the claude -p command (use for tests / smoke probes).",
    )
    return parser


def dispatch_cancel(
    *,
    dispatch_id: str,
    workspace_root: Path | None = None,
    sigterm_grace_seconds: float = SIGTERM_GRACE_SECONDS,
    _kill_pg_fn: Any = None,
    _is_alive_fn: Any = None,
    _sleep_fn: Any = None,
) -> dict[str, Any]:
    """Cancel a running dispatch via the SIGTERM → 5s grace → SIGKILL kill ladder.

    Returns a dict with ``status`` one of:

    - ``cancelled``   — kill ladder ran; workspace wiped.
    - ``noop``        — dispatch was already done, not found, or had no pid.

    Extra keys on ``cancelled``:

    - ``exitcode``     — 143 (SIGTERM) or 137 (SIGKILL).
    - ``force_killed`` — True if SIGKILL was needed (grace period expired).
    - ``elapsed``      — human-readable wall time from ``started_at`` to now.
    - ``cost``         — partial cost from last babysit frame (if any).
    - ``was_queued``   — True when the dispatch existed but never got a pid.

    The workspace directory is wiped after the kill so the operator's
    filesystem is clean. State files (``exitcode``, ``cancel_reason``)
    are written first for the brief window before the wipe, and so any
    in-flight supervision tick that races us sees terminal state instead
    of a half-dead orphan.
    """
    kill_pg = _kill_pg_fn or _kill_pg
    is_alive = _is_alive_fn or _is_pid_alive
    sleep = _sleep_fn or time.sleep

    root = workspace_root if workspace_root is not None else _workspace_root()
    workspace = root / dispatch_id

    if not workspace.is_dir():
        return {"status": "noop", "reason": "not_found", "dispatch_id": dispatch_id}

    def _read(field: str) -> str | None:
        p = workspace / field
        try:
            return p.read_text().strip() or None
        except FileNotFoundError:
            return None
        except OSError:
            return None

    # Already terminal — supervision already posted; nothing to kill.
    if _read("exitcode") is not None:
        return {"status": "noop", "reason": "not_running", "dispatch_id": dispatch_id}

    pid_str = _read("pid")
    started_at_str = _read("started_at")
    cost = _read("cost")

    # Queued but never started — workspace exists, no pid written yet.
    if pid_str is None:
        try:
            _atomic_write(workspace / "cancel_reason", "user_cancel")
            _atomic_write(workspace / "exitcode", str(EXITCODE_SIGTERM))
        except OSError:
            pass
        shutil.rmtree(workspace, ignore_errors=True)
        return {
            "status": "cancelled",
            "dispatch_id": dispatch_id,
            "was_queued": True,
            "note": "was queued, never started",
        }

    try:
        pid = int(pid_str)
    except (TypeError, ValueError):
        return {"status": "noop", "reason": "invalid_pid", "dispatch_id": dispatch_id}

    if pid <= 0:
        return {"status": "noop", "reason": "invalid_pid", "dispatch_id": dispatch_id}

    # Compute elapsed before we wipe started_at.
    now = datetime.now(timezone.utc)
    elapsed: str | None = None
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str)
            elapsed = _format_elapsed((now - started_at).total_seconds())
        except (ValueError, TypeError):
            pass

    # Kill ladder: SIGTERM → grace period → SIGKILL if still alive.
    kill_pg(pid, signal.SIGTERM)

    deadline = time.monotonic() + sigterm_grace_seconds
    while time.monotonic() < deadline:
        if not is_alive(pid):
            break
        sleep(0.1)

    force_killed = False
    if is_alive(pid):
        kill_pg(pid, signal.SIGKILL)
        force_killed = True
        exitcode_int = EXITCODE_SIGKILL
    else:
        exitcode_int = EXITCODE_SIGTERM

    # Write terminal state files before wiping so a racing supervisor tick
    # sees terminal state rather than an empty dir.
    try:
        _atomic_write(workspace / "exitcode", str(exitcode_int))
        _atomic_write(workspace / "cancel_reason", "user_cancel")
    except OSError:
        logger.warning("dispatch_cancel: could not write state files for %s", dispatch_id)

    # Wipe the workspace — workspace + any auth/ subdir, per spec.
    shutil.rmtree(workspace, ignore_errors=True)

    result: dict[str, Any] = {
        "status": "cancelled",
        "dispatch_id": dispatch_id,
        "exitcode": exitcode_int,
        "force_killed": force_killed,
    }
    if elapsed is not None:
        result["elapsed"] = elapsed
    if cost:
        result["cost"] = cost
    return result


def _build_cancel_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch.handler dispatch_cancel", add_help=False)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument(
        "--sigterm-grace-seconds",
        type=float,
        default=SIGTERM_GRACE_SECONDS,
        help="Seconds to wait between SIGTERM and SIGKILL (default: 5).",
    )
    return parser


def _parse_args(argv: list[str]) -> tuple[str, list[str]]:
    """Split ``[verb, *rest]``. Mirrors the original D-1 contract so an
    unknown verb still routes through our JSON error printer rather than
    argparse's stderr usage message."""
    if not argv:
        raise SystemExit("dispatch.handler: missing verb argument")
    return argv[0], argv[1:]


def run(argv: list[str] | None = None) -> int:
    """Run the handler. Returns the exit code. Public so tests can drive it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    verb, rest = _parse_args(argv if argv is not None else sys.argv[1:])

    if verb == "dispatch_health":
        print(json.dumps(dispatch_health()))
        return EXIT_OK

    if verb == "dispatch_issue":
        args = _build_issue_parser().parse_args(rest)
        try:
            channel, thread_ts, agent = _resolve_slack_context(
                channel=args.channel,
                thread_ts=args.thread_ts,
                agent=args.agent,
            )
        except ValueError as e:
            print(json.dumps({"error": "missing_slack_context", "message": str(e)}))
            return EXIT_USAGE
        result = dispatch_issue(
            issue_url=args.issue_url,
            channel=channel,
            thread_ts=thread_ts,
            agent=agent,
            budget_seconds=args.budget_seconds,
            model=args.model,
            persona=args.persona,
            exec_override=args.exec_override,
            supervision_mode=args.supervision_mode,
        )
        print(json.dumps(result))
        # ``launched`` (poll) and ``completed`` (inline) are both
        # successful outcomes for the CLI — exit 0. ``launch_failed``
        # and ``failed`` (nonzero exitcode in inline) are errors.
        ok_statuses = {"launched", "completed"}
        return EXIT_OK if result.get("status") in ok_statuses else EXIT_LAUNCH_FAILED

    if verb == "dispatch_cancel":
        args = _build_cancel_parser().parse_args(rest)
        result = dispatch_cancel(
            dispatch_id=args.dispatch_id,
            sigterm_grace_seconds=args.sigterm_grace_seconds,
        )
        print(json.dumps(result))
        # Both ``cancelled`` and ``noop`` are non-error outcomes — the
        # caller asked to cancel and the dispatch is now not running.
        return EXIT_OK

    print(
        json.dumps(
            {
                "error": "unknown_verb",
                "verb": verb,
                "message": (
                    "Known verbs: dispatch_health, dispatch_issue, dispatch_cancel. "
                    "dispatch_status lands in its own issue."
                ),
            }
        )
    )
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
