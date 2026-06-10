"""CLI entry point for the dispatch pack.

The agent invokes this script from Bash:

    python /config/packs/dispatch/handler.py <verb> [--<arg> ...]

Verbs:

- ``dispatch_health``   — smoke probe.
- ``dispatch_issue``    — primary verb, spawns a headless claude session
                          and returns ``{status: "launched", dispatch_id}``
                          in <3s; supervision lives router-side (#163).
- ``dispatch_status``   — pool snapshot: {running: [...], queued: [...]}.
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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import URLError

from constants import POOL_SLOTS_DIR_NAME

# D-5: Quota telemetry module — co-located with the handler so the pack
# ships as a single directory. Falls back gracefully if quota.py is
# absent during a zero-downtime upgrade.
try:
    import quota as _quota

    _QUOTA_AVAILABLE = True
except ImportError:
    _quota = None  # type: ignore[assignment]
    _QUOTA_AVAILABLE = False

# Config path: resolves to /config/dispatch.yaml in the deployed Sam container
# (where the pack ships as /config/packs/dispatch/handler.py) and to
# <repo-root>/dispatch.yaml in dev. Override via QUOTA_CONFIG_PATH env var.
_QUOTA_CONFIG_PATH = (
    Path(os.environ["QUOTA_CONFIG_PATH"])
    if "QUOTA_CONFIG_PATH" in os.environ
    else Path(__file__).parent.parent.parent / "dispatch.yaml"
)


def _load_quota_config() -> dict:
    if not _QUOTA_AVAILABLE or _quota is None:
        return {"threshold_usd": 50.0, "window_hours": 5.0}
    return _quota.load_config(_QUOTA_CONFIG_PATH)


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

# D-6: Janitor startup sweep — run once per process, never on
# dispatch_health (which must never be blocked by the sweep).
_janitor_lock = threading.Lock()
_janitor_done = False


def _maybe_run_janitor() -> None:
    global _janitor_done
    if _janitor_done:
        return
    with _janitor_lock:
        if _janitor_done:
            return
        try:
            from janitor import sweep  # co-located pack module

            sweep()
        except Exception as e:
            logger.warning("janitor sweep failed (non-fatal): %s", e)
        _janitor_done = True


# Supervision mode toggle from #163's rollback section. ``inline`` runs
# the babysit foreground and blocks until ``exitcode`` lands, then
# returns the terminal envelope inline — that's the pre-#163 behavior
# preserved for safety. ``poll`` detaches the babysit and relies on the
# router-side discovery + supervision loops to surface the result back
# to the Slack thread. Default flipped to ``poll`` (issue #212 follow-up,
# 2026-05-20): every production caller — router approval path, agent-driven
# dispatch, scheduled tasks — wants ``poll`` and a missing
# ``$DISPATCH_SUPERVISION`` in the agent container env should not fall
# through to inline-and-block-the-router. Operators can still force
# ``inline`` by setting ``DISPATCH_SUPERVISION=inline`` on the agent
# container without a code change.
SUPERVISION_MODE_ENV = "DISPATCH_SUPERVISION"
SUPERVISION_MODE_INLINE = "inline"
SUPERVISION_MODE_POLL = "poll"
DEFAULT_SUPERVISION_MODE = SUPERVISION_MODE_POLL

# ── D-3: Slot pool, FIFO queue, auth isolation ───────────────────────────

# Hard concurrency cap. At most POOL_SIZE ``claude -p`` subprocesses run
# in parallel; excess requests queue FIFO (design doc §Concurrency).
POOL_SIZE = 3

# Hidden subdirs under the workspace root for pool bookkeeping. Leading
# dot keeps them out of list_dispatch_ids() and the dispatch namespace.
POOL_QUEUE_DIR_NAME = ".queue"

# Per-dispatch auth subdirectory, seeded by copying the canonical creds.
AUTH_SEED_DIR_NAME = "auth"

# Canonical Claude CLI credentials directory. Defaults to ~/.claude/ —
# the same default the CLI itself uses. Each dispatch gets a fresh copy
# to avoid the concurrent token-refresh race described in the design doc.
CANONICAL_CLAUDE_DIR_ENV = "CLAUDE_CONFIG_DIR"

# How often to poll for a free slot while queued (seconds).
POOL_POLL_INTERVAL = 1.0

# GitHub machine-user PAT for the dispatch worker pool (issue #227).
# Bram drops this token at /config/secrets/gh-aidt-dispatch.token (0600,
# root-owned) after creating the aidt-dispatch GitHub account manually.
# Override path via env var for testing.
DISPATCH_TOKEN_PATH = "/config/secrets/gh-aidt-dispatch.token"
DISPATCH_TOKEN_PATH_ENV = "DISPATCH_TOKEN_PATH"
DISPATCH_GIT_NAME = "Dispatch (aidt-dispatch)"
DISPATCH_GIT_EMAIL = "aidt-dispatch@users.noreply.github.com"

# Worker Slack bot token — injected into the worker container by the router
# from the ``workers_bot_token`` secret (see router/packs/dispatch_hook.py,
# issue #250). This token is used for all worker-originated Slack posts so
# posts appear under the workers_bot_id rather than the agent identity.
# When this token is absent and the dispatch is configured to post, the
# dispatch fails fast rather than silently falling back to the agent token
# (the silent-fallback is the root cause of the #241 / #245 regressions).
WORKERS_BOT_TOKEN_ENV = "WORKERS_BOT_TOKEN"
# Kept for reference / healthz compatibility; no longer used for posting.
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

# D-7: Approval cost-gate threshold. Absolute USD spend in the current
# 5h window that triggers an approval card regardless of model/keywords.
# Default $15; tweak via env (restart agent after edit, no code change needed).
DISPATCH_APPROVAL_COST_USD_ENV = "DISPATCH_APPROVAL_COST_USD"
DEFAULT_APPROVAL_COST_THRESHOLD_USD = 15.0

# Regex matching credential-like tokens (GitHub PATs, Slack tokens, etc.) so
# they can be stripped before detail strings reach Slack.
_SECRET_RE = re.compile(
    r"\b(?:ghp|ghs|gho|github_pat|xoxb|xoxe|xoxa|xoxp)_[A-Za-z0-9_]{10,}\b"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z0-9+/]{40,})(?![A-Za-z0-9+/=])"
)
_DETAIL_MAX_LEN = 300


def _redact_detail(text: str) -> str:
    """Strip secrets and truncate *text* for safe inclusion in Slack messages."""
    redacted = _SECRET_RE.sub("[REDACTED]", text)
    if len(redacted) > _DETAIL_MAX_LEN:
        redacted = redacted[:_DETAIL_MAX_LEN] + "…"
    return redacted


def _read_machine_user_token(path: str | Path | None = None) -> str | None:
    """Read the aidt-dispatch GitHub PAT from the secrets file.

    Skips comment lines (``#``-prefixed) and blank lines so placeholder
    files produced by ``make seed-config`` are silently ignored.
    Returns ``None`` when the file is absent, unreadable, or contains
    only comments/blanks.
    """
    resolved = Path(path) if path is not None else Path(os.environ.get(DISPATCH_TOKEN_PATH_ENV, DISPATCH_TOKEN_PATH))
    try:
        content = resolved.read_text()
    except FileNotFoundError:
        logger.info("dispatch token file not present at %s", resolved)
        return None
    except PermissionError:
        logger.warning(
            "dispatch token file unreadable (uid=%d, path=%s); check ownership",
            os.getuid(),
            resolved,
        )
        return None
    except OSError as exc:
        logger.warning("dispatch token file unreadable: %s", exc)
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    logger.warning("dispatch token file empty")
    return None


def _seed_dispatch_identity(workspace: Path, token: str, *, dispatch_repo: "Path | None" = None) -> None:
    """Write per-dispatch .env and .gitconfig for the aidt-dispatch identity.

    * ``workspace/.env`` (mode 0600) — ``GH_TOKEN``, ``GITHUB_TOKEN``,
      and (when provided) ``DISPATCH_REPO`` pointing at the pre-cloned repo.
    * ``workspace/.gitconfig`` — ``user.name`` / ``user.email`` for
      commit authorship.

    The host agent's own PAT is never written here; the caller is
    responsible for stripping ``GH_TOKEN``/``GITHUB_TOKEN`` from the
    subprocess environment before passing ``workspace/.env`` values in.
    """
    env_path = workspace / ".env"
    env_content = f"GH_TOKEN={token}\nGITHUB_TOKEN={token}\n"
    if dispatch_repo is not None:
        env_content += f"DISPATCH_REPO={dispatch_repo}\n"
    env_path.write_text(env_content)
    env_path.chmod(0o600)

    gitconfig_path = workspace / ".gitconfig"
    gitconfig_path.write_text(f"[user]\n\tname = {DISPATCH_GIT_NAME}\n\temail = {DISPATCH_GIT_EMAIL}\n")


def _clone_repo_into_workspace(workspace: Path, issue_url: str, *, run: Any = subprocess.run) -> Path:
    """Clone origin/main into ``<workspace>/repo/`` and return the repo path.

    Parses ``owner/repo`` from ``issue_url``; no new config required.
    Raises ``RuntimeError`` on any git failure so the caller can surface a
    structured error before the slot is acquired.
    """
    # Parse owner/repo from https://github.com/owner/repo/issues/N
    gh_prefix = "github.com/"
    idx = issue_url.find(gh_prefix)
    if idx == -1:
        raise RuntimeError(f"cannot parse owner/repo from issue_url: {issue_url!r}")
    parts = issue_url[idx + len(gh_prefix) :].split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise RuntimeError(f"cannot parse owner/repo from issue_url: {issue_url!r}")
    owner, repo_name = parts[0], parts[1]

    clone_url = f"https://github.com/{owner}/{repo_name}.git"
    repo_path = workspace / "repo"

    for cmd in [
        ["git", "clone", "--depth=50", clone_url, str(repo_path)],
        ["git", "-C", str(repo_path), "checkout", "main"],
        ["git", "-C", str(repo_path), "pull", "--ff-only"],
    ]:
        result = run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(f"{cmd[0]} {cmd[1]} failed: {detail}")

    return repo_path


def _check_gh_auth(*, run: Any = subprocess.run) -> tuple[bool, str | None]:
    """Run ``gh auth status`` and return ``(ok, detail)``.

    Returns ``(True, None)`` on zero exit; ``(False, detail)`` otherwise.
    Never raises — all failures are surfaced as ``(False, …)``.
    """
    try:
        completed = run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, "gh binary not found"
    except subprocess.TimeoutExpired:
        return False, "gh auth status timed out"
    except OSError as e:
        return False, str(e)

    if completed.returncode == 0:
        return True, None
    detail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-200:]
    return False, detail or "gh auth status exited non-zero"


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
    _gh_run: Any = subprocess.run,
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
          "gh_auth_ok": bool,
          "gh_auth_detail": str | None,           # only on failure
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
    gh_ok, gh_detail = _check_gh_auth(run=_gh_run)

    out: dict[str, Any] = {
        "cli_version": cli_version,
        "claude_path": claude_path,
        "workspace_volume_writable": writable,
        "sonnet_probe_ok": sonnet_ok,
        "sonnet_probe_exit_code": sonnet_exit,
        "gh_auth_ok": gh_ok,
    }
    if not sonnet_ok:
        out["sonnet_probe_detail"] = sonnet_detail
    if not gh_ok:
        out["gh_auth_detail"] = gh_detail

    # D-5: Quota telemetry fields.
    if _QUOTA_AVAILABLE and _quota is not None:
        _now = datetime.now(timezone.utc)
        _cfg = _load_quota_config()
        _w_cost, _w_count, _ = _quota.window_state(root, _now, window_hours=_cfg["window_hours"])
        _locked, _retry_after = _quota.is_locked(root, _now, window_hours=_cfg["window_hours"])
        out["window_cost_usd"] = round(_w_cost, 4)
        out["dispatches_this_window"] = _w_count
        out["quota_locked"] = _locked
        if _locked and _retry_after:
            out["quota_retry_after"] = _retry_after
    else:
        out["window_cost_usd"] = 0.0
        out["dispatches_this_window"] = 0
        out["quota_locked"] = False

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


# ── D-3: Auth seeding ────────────────────────────────────────────────────


def _canonical_auth_dir() -> Path:
    """Return the canonical Claude CLI credentials directory.

    Resolution order: ``$CLAUDE_CONFIG_DIR`` env var, then ``~/.claude``.
    Does not verify the directory exists.
    """
    env_val = os.environ.get(CANONICAL_CLAUDE_DIR_ENV)
    if env_val:
        return Path(env_val)
    return Path.home() / ".claude"


def _seed_auth_dir(
    workspace: Path,
    *,
    copy_fn: Any = None,
) -> Path:
    """Copy canonical Claude creds into ``<workspace>/auth/``.

    Uses ``shutil.copytree`` with ``dirs_exist_ok=True`` so re-seeding
    is idempotent. Raises ``RuntimeError`` on ANY copy failure — callers
    surface ``{status: error, reason: auth_seed_failed}`` and must NOT
    fall back to the shared ``~/.claude/``.

    ``copy_fn`` is injectable for tests (pass a no-op or a spy); defaults
    to ``shutil.copytree``.
    """
    src = _canonical_auth_dir()
    dst = workspace / AUTH_SEED_DIR_NAME
    dst.mkdir(exist_ok=True, mode=0o700)
    copytree = copy_fn if copy_fn is not None else shutil.copytree
    try:
        copytree(src, dst, dirs_exist_ok=True)
    except TypeError:
        # Older shutil.copytree doesn't support dirs_exist_ok; retry without.
        try:
            copytree(src, dst)
        except (FileNotFoundError, OSError, shutil.Error) as e:
            raise RuntimeError(f"auth_seed_failed: {e}") from e
    except FileNotFoundError as e:
        raise RuntimeError(f"auth_seed_failed: canonical dir missing: {e}") from e
    except (OSError, shutil.Error) as e:
        raise RuntimeError(f"auth_seed_failed: {e}") from e
    return dst


# ── D-3: Slot pool ───────────────────────────────────────────────────────


def _slots_dir(root: Path) -> Path:
    """Return (and ensure) the pool slot-lock directory under ``root``."""
    d = root / POOL_SLOTS_DIR_NAME
    d.mkdir(exist_ok=True, mode=0o700)
    return d


def _queue_dir(root: Path) -> Path:
    """Return (and ensure) the FIFO queue ticket directory under ``root``."""
    d = root / POOL_QUEUE_DIR_NAME
    d.mkdir(exist_ok=True, mode=0o700)
    return d


def _try_acquire_slot(slots_dir: Path, dispatch_id: str) -> int | None:
    """Atomically claim the lowest-numbered free slot via ``O_CREAT|O_EXCL``.

    Returns the slot index (0-based) on success, ``None`` when all
    ``POOL_SIZE`` slots are occupied. The slot file stores the owning
    ``dispatch_id`` for diagnostics; the file's mere existence is the
    lock.
    """
    for i in range(POOL_SIZE):
        slot_path = slots_dir / f"slot-{i}"
        try:
            fd = os.open(str(slot_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, dispatch_id.encode())
            finally:
                os.close(fd)
            return i
        except FileExistsError:
            continue
    return None


def _release_slot(slots_dir: Path, slot_idx: int) -> None:
    """Release a slot by removing its lock file. Idempotent."""
    slot_path = slots_dir / f"slot-{slot_idx}"
    try:
        slot_path.unlink()
    except FileNotFoundError:
        pass


def _release_slot_for_dispatch(slots_dir: Path, dispatch_id: str) -> None:
    """Release whichever slot file holds this dispatch_id. Idempotent.

    Used by cancel paths that know the dispatch_id but not the slot index.
    Scans all slot-N files and removes the one whose body matches.
    """
    if not slots_dir.exists():
        return
    for p in slots_dir.iterdir():
        if not (p.is_file() and p.name.startswith("slot-")):
            continue
        try:
            if p.read_text().strip() == dispatch_id:
                p.unlink(missing_ok=True)
                return
        except OSError:
            continue


def _count_active_slots(slots_dir: Path) -> int:
    """Return the number of slot-lock files currently present."""
    if not slots_dir.exists():
        return 0
    return sum(1 for p in slots_dir.iterdir() if p.is_file() and p.name.startswith("slot-"))


# ── D-3: FIFO queue ──────────────────────────────────────────────────────


def _queue_ticket(queue_dir: Path, dispatch_id: str) -> Path:
    """Create a FIFO queue ticket. The sortable name (``<ns_timestamp>-<id>``)
    determines FIFO order when multiple dispatches queue simultaneously.
    """
    ticket_name = f"{time.time_ns():020d}-{dispatch_id}"
    ticket_path = queue_dir / ticket_name
    ticket_path.write_text(dispatch_id)
    return ticket_path


def _queue_position(queue_dir: Path, ticket_name: str) -> int:
    """Return how many tickets are ahead of ours (0 = front of queue)."""
    try:
        tickets = sorted(p.name for p in queue_dir.iterdir() if p.is_file())
    except (FileNotFoundError, OSError):
        return 0
    try:
        return tickets.index(ticket_name)
    except ValueError:
        return 0  # ticket removed or not found — treat as front


# ── D-3: Slack status messages ───────────────────────────────────────────


def _post_slack_message(
    channel: str,
    thread_ts: str,
    text: str,
    *,
    token: str | None = None,
    _urlopen: Any = None,
    dispatch_id: str = "",
    persona: str = "",
) -> bool:
    """Post a Slack message via WORKERS_BOT_TOKEN.

    Raises ``RuntimeError`` when ``WORKERS_BOT_TOKEN`` is absent from the
    environment, no injectable ``token`` was provided, and ``channel`` is
    non-empty — this is intentional fail-fast behaviour to prevent the
    dispatch from silently posting under the agent identity (see #241).

    Returns ``True`` on success, ``False`` when posting is not applicable
    (empty channel) or on transient network errors.

    Raises ``RuntimeError`` for ``not_in_channel`` Slack API errors so
    callers can surface the problem via the dispatch-error path rather than
    swallowing it.
    """
    # WORKERS_BOT_TOKEN wins over the injectable (test) token when both present.
    tok = os.environ.get(WORKERS_BOT_TOKEN_ENV) or token
    if not tok:
        if not channel:
            logger.debug("Slack post skipped: no channel")
            return False
        raise RuntimeError("WORKERS_BOT_TOKEN not set; refusing to fall back to agent token (see #241)")
    if not channel:
        return False

    prefix = f"[dispatch-{dispatch_id} · {persona}] " if dispatch_id and persona else ""
    payload: dict[str, Any] = {"channel": channel, "text": prefix + text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = json.dumps(payload).encode()
    req = urlrequest.Request(
        SLACK_API_POST_MESSAGE,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
        },
    )
    opener = _urlopen or urlrequest.urlopen
    try:
        resp = opener(req, timeout=5)
        if resp is not None:
            try:
                body = json.loads(resp.read().decode())
                if not body.get("ok") and body.get("error") == "not_in_channel":
                    raise RuntimeError(f"Slack not_in_channel: worker bot is not a member of {channel!r}")
            except (AttributeError, ValueError, UnicodeDecodeError):
                pass
        return True
    except RuntimeError:
        raise
    except (URLError, OSError) as e:
        logger.warning("Slack post failed (channel=%s): %s", channel, e)
        return False


# ── D-3: Slot acquisition (blocks until a slot is available) ─────────────


def _acquire_slot(
    root: Path,
    dispatch_id: str,
    *,
    channel: str = "",
    thread_ts: str = "",
    persona: str = "",
    poll_interval: float = POOL_POLL_INTERVAL,
    slack_token: str | None = None,
    _sleep_fn: Any = None,
) -> tuple[int, int]:
    """Block until a slot is available, managing the FIFO queue as needed.

    Returns ``(slot_idx, slot_num)`` where ``slot_idx`` is 0-based (used
    to name the lock file) and ``slot_num`` is 1-based (used in Slack
    messages like "started — slot 2/3").

    Algorithm:
    1. Fast path — try to grab any free slot immediately. Post "started"
       and return if successful (no queue entry created).
    2. Slow path — all slots busy. Create a FIFO queue ticket. Poll until
       we're at the front of the queue AND a slot opens up. Post "queued"
       once on entry; post "started" when promoted. Remove ticket in the
       ``finally`` block so a crash or SIGTERM doesn't leave stale tickets.
    """
    sleep = _sleep_fn or time.sleep
    slots_dir = _slots_dir(root)
    queue_dir = _queue_dir(root)

    # Fast path: slot available right now.
    slot_idx = _try_acquire_slot(slots_dir, dispatch_id)
    if slot_idx is not None:
        slot_num = slot_idx + 1
        _post_slack_message(
            channel,
            thread_ts,
            f"started — slot {slot_num}/{POOL_SIZE}",
            token=slack_token,
            dispatch_id=dispatch_id,
            persona=persona,
        )
        return slot_idx, slot_num

    # Slow path: join the FIFO queue.
    ticket = _queue_ticket(queue_dir, dispatch_id)
    running = _count_active_slots(slots_dir)

    try:
        tickets = sorted(queue_dir.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, OSError):
        tickets = [ticket]
    my_pos = next((i for i, t in enumerate(tickets) if t.name == ticket.name), 0)

    _post_slack_message(
        channel,
        thread_ts,
        f"queued — slot {running} of {POOL_SIZE} in use, {my_pos} ahead in queue",
        token=slack_token,
        dispatch_id=dispatch_id,
        persona=persona,
    )
    logger.info(
        "dispatch %s queued: %d/%d slots in use, position %d",
        dispatch_id,
        running,
        POOL_SIZE,
        my_pos,
    )

    try:
        while True:
            sleep(poll_interval)

            try:
                my_pos = _queue_position(queue_dir, ticket.name)
            except Exception:
                my_pos = 0

            # Only the front-of-queue waiter attempts slot acquisition to
            # preserve FIFO order under concurrent contention.
            if my_pos == 0:
                slot_idx = _try_acquire_slot(slots_dir, dispatch_id)
                if slot_idx is not None:
                    slot_num = slot_idx + 1
                    _post_slack_message(
                        channel,
                        thread_ts,
                        f"started — slot {slot_num}/{POOL_SIZE}",
                        token=slack_token,
                        dispatch_id=dispatch_id,
                        persona=persona,
                    )
                    logger.info(
                        "dispatch %s acquired slot %d/%d",
                        dispatch_id,
                        slot_num,
                        POOL_SIZE,
                    )
                    return slot_idx, slot_num
    finally:
        # Always remove the queue ticket — even on interrupt or exception —
        # so stale tickets never block future dispatches.
        try:
            ticket.unlink()
        except (FileNotFoundError, OSError):
            pass


# ── D-7: Approval gating ─────────────────────────────────────────────────────


def _load_approval_config() -> dict:
    """Load approval config from dispatch.yaml. Fails closed (require_always=true) on any error."""
    if not _QUOTA_AVAILABLE or _quota is None:
        return {
            "require_always": True,
            "destructive_keywords": ["destructive", "delete", "drop", "migration", "reset"],
        }
    return _quota.load_approval_config(_QUOTA_CONFIG_PATH)


def _approval_cost_threshold() -> float:
    """Return the cost-gate threshold from env or default $15. Warns + falls back on bad value."""
    raw = os.environ.get(DISPATCH_APPROVAL_COST_USD_ENV, "").strip()
    if not raw:
        return DEFAULT_APPROVAL_COST_THRESHOLD_USD
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning(
            "%s=%r is not a valid float; falling back to $%.2f default",
            DISPATCH_APPROVAL_COST_USD_ENV,
            raw,
            DEFAULT_APPROVAL_COST_THRESHOLD_USD,
        )
        return DEFAULT_APPROVAL_COST_THRESHOLD_USD


def _extract_repo(issue_url: str) -> str:
    """Extract 'owner/repo' from a github.com issue URL. Returns '' on failure."""
    try:
        parts = issue_url.rstrip("/").split("/")
        gh_idx = next(i for i, p in enumerate(parts) if "github.com" in p)
        return f"{parts[gh_idx + 1]}/{parts[gh_idx + 2]}"
    except (StopIteration, IndexError):
        return ""


def _fetch_issue_text(issue_url: str, *, run: Any = subprocess.run) -> str:
    """Fetch issue title+body via gh CLI. Returns empty string on any failure."""
    try:
        result = run(
            ["gh", "issue", "view", issue_url, "--json", "title,body"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return ""
        data = json.loads(result.stdout or "{}")
        title = data.get("title", "") or ""
        body = data.get("body", "") or ""
        return f"{title}\n{body}"
    except Exception:
        return ""


def _evaluate_approval_gate(
    *,
    issue_url: str,
    model: str,
    root: Path,
    now: datetime,
    approval_cfg: dict,
    cost_threshold: float,
    fetch_fn: Any = None,
) -> dict[str, Any] | None:
    """Decide whether this dispatch needs human approval.

    Returns a preview dict (gate fired) or ``None`` (run directly).
    ``preview`` is the payload for the Slack approval card.
    """
    repo = _extract_repo(issue_url)
    est_dispatch_id = f"dispatch-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    base_preview: dict[str, Any] = {
        "repo": repo,
        "issue_url": issue_url,
        "branch_target": "main",
        "model": model,
        "est_workspace_path": str(_workspace_root() / est_dispatch_id),
    }

    if approval_cfg.get("require_always"):
        return {**base_preview, "gate_reason": "always"}

    # Smart-gate: model=opus AND destructive keyword in issue text.
    if model == "opus":
        fetcher = fetch_fn if fetch_fn is not None else _fetch_issue_text
        issue_text = fetcher(issue_url).lower()
        keywords = [k.lower() for k in approval_cfg.get("destructive_keywords", [])]
        matched = next((k for k in keywords if k in issue_text), None)
        if matched is not None:
            return {**base_preview, "gate_reason": "destructive_keyword", "matched_keyword": matched}

    # Smart-gate: 5h window cost ≥ threshold.
    if _QUOTA_AVAILABLE and _quota is not None:
        _cfg = _load_quota_config()
        window_cost, _, _ = _quota.window_state(root, now, window_hours=_cfg["window_hours"])
        if window_cost >= cost_threshold:
            return {
                **base_preview,
                "gate_reason": "cost_threshold",
                "current_window_cost_usd": round(window_cost, 4),
                "threshold_usd": cost_threshold,
            }

    return None


def _build_claude_command(
    *,
    issue_url: str,
    model: str,
    persona: str,
    workspace: Path,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Render the ``claude -p`` invocation the babysit will exec."""
    # Worker contract — keep in sync with packs/dispatch/README.md
    # ("Worker contract"). The "push before you verify" rule is the
    # standing fix for #203 ``ccc4ec`` and #154: when a dispatch is
    # killed mid-loop (stuck guard, budget, timeout) the work survives
    # in git instead of being stranded in ``/var/lib/dispatch/<id>/``.
    prompt = (
        f"Work on GitHub issue {issue_url} as persona={persona}. "
        f"Read the issue, implement the requested change in this workspace, "
        f"commit on a fresh branch, push, and open a pull request.\n\n"
        f"Worker contract:\n"
        f"1. Push before you verify. As soon as the change compiles and your "
        f"NEW tests pass, commit and push the branch. Open the PR as a draft "
        f"if it isn't done yet. Only run broader integration tests AFTER the "
        f"branch is pushed. If you get killed mid-loop, the work survives in "
        f"git instead of stranded in this workspace.\n"
        f"2. Ignore pre-existing test failures unrelated to your change. Do "
        f"not chase them. Confirm they exist on main and move on; do not "
        f"loop trying to verify or fix them.\n"
        f"3. Scope discipline. Touch only what the issue asks for. If the "
        f"issue body says ``do not touch X``, that is binding.\n"
        f"4. CI green is the definition of done. Before declaring the PR "
        f"ready or reporting back ``done``, run the repo's lint and format "
        f"checks locally and fix any failures. For Python repos in this "
        f"org that means BOTH ``ruff check .`` AND ``ruff format --check "
        f".`` — CI runs both and a passing check with a failing "
        f"format-check still fails the lint job. Tests passing is not "
        f"enough; lint is part of the contract.\n\n"
        f"The repo is pre-cloned at ``$DISPATCH_REPO`` — work there. "
        f"Do not create other scratch paths (no /tmp/sam-scratch or similar)."
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
    # D-3 injectable seams (for tests and smoke probes).
    _seed_auth_fn: Any = None,
    _sleep_fn: Any = None,
    _slack_token: str | None = None,
    # D-7: approval gate.
    _approved: bool = False,
    _fetch_issue_fn: Any = None,
    _approval_cfg: dict | None = None,
    # Issue #227: dispatch identity injection. Pass a path to override the
    # default /config/secrets/gh-aidt-dispatch.token (for unit tests).
    _dispatch_token_path: str | Path | None = None,
    # Issue #307: per-dispatch repo clone. Injectable for unit tests.
    _clone_repo_fn: Any = None,
) -> dict[str, Any]:
    """Launch a headless dispatch.

    Behavior depends on the supervision mode (set explicitly here or
    resolved from ``$DISPATCH_SUPERVISION``, default ``inline``):

    * **inline** (default) — run the babysit foreground, wait for
      ``exitcode`` to land, return ``{status: "completed", exitcode,
      ...}`` with the full terminal envelope.
    * **poll** — write the launch sidecar state, spawn the babysit
      detached (``start_new_session=True``), and return
      ``{status: "launched", dispatch_id, ...}`` in <3s.

    D-3 additions:

    * Auth isolation — copies ``~/.claude/`` into ``<workspace>/auth/``
      before spawning the babysit. Fails fast with
      ``{status: error, reason: auth_seed_failed}`` on any copy error;
      never falls back to the shared ``~/.claude/``. Skipped when
      ``exec_override`` is set (test / smoke-probe mode).
    * Slot pool — acquires one of ``POOL_SIZE`` (=3) slots before
      spawning. Blocks (with FIFO queuing) when all slots are busy.
      Posts Slack "queued" / "started" messages via ``WORKERS_BOT_TOKEN``.

    D-7 additions:

    * Approval gate — evaluated before workspace creation or slot
      acquisition. Returns ``{status: "approval_required", draft_id,
      preview}`` when the gate fires. Pass ``_approved=True`` (set by
      the router on re-invocation after human Approve click) to bypass
      the gate; logged as ``gate_bypass_via_approval``.

    Issue #227 additions:

    * Dispatch identity — reads ``/config/secrets/gh-aidt-dispatch.token``
      and writes ``workspace/.env`` (0600) + ``workspace/.gitconfig`` so
      the worker authenticates as ``aidt-dispatch``. The host agent's own
      ``GH_TOKEN``/``GITHUB_TOKEN`` are stripped from the subprocess
      environment so the host PAT never reaches the worker. Skipped when
      ``exec_override`` is set (test / smoke-probe mode).

    ``exec_override`` exists for tests and the smoke probe: pass a list
    like ``["sleep", "30"]`` and the babysit will run that instead of
    the real ``claude -p`` command.

    ``_seed_auth_fn``, ``_sleep_fn``, ``_slack_token`` are injectable
    for unit tests so they can skip file copies, avoid real sleeps, and
    suppress Slack HTTP calls.

    ``_fetch_issue_fn``, ``_approval_cfg`` are injectable for D-7 tests.

    ``_dispatch_token_path`` overrides the default secrets path for tests.

    Issue #307 additions:

    * Per-dispatch repo clone — clones ``origin/main`` into
      ``<workspace>/repo/`` before exec, making a clean, freshly-fetched
      tree available at ``$DISPATCH_REPO``. Fails fast before slot acquire
      on git errors. Skipped when ``exec_override`` is set. Injectable
      via ``_clone_repo_fn`` for unit tests.
    """
    mode = (supervision_mode or _supervision_mode()).lower()
    now = now or datetime.now(timezone.utc)
    root = workspace_root if workspace_root is not None else _workspace_root()

    # D-7: Evaluate approval gate before any workspace state is written
    # or any slot is acquired. The gate is always skipped when _approved
    # is True (router re-invocation after human Approve click).
    approval_cfg = _approval_cfg if _approval_cfg is not None else _load_approval_config()
    if _approved:
        logger.info("gate_bypass_via_approval: dispatch_issue invoked with _approved=True")
    else:
        gate_preview = _evaluate_approval_gate(
            issue_url=issue_url,
            model=model,
            root=root,
            now=now,
            approval_cfg=approval_cfg,
            cost_threshold=_approval_cost_threshold(),
            fetch_fn=_fetch_issue_fn,
        )
        if gate_preview is not None:
            return {
                "status": "approval_required",
                "draft_id": uuid.uuid4().hex[:8],
                "preview": gate_preview,
            }
    # Startup fail-fast: if posting is required (channel is set) but
    # WORKERS_BOT_TOKEN is absent and no injectable token was provided,
    # refuse immediately rather than silently posting under the agent identity
    # (see #241 for the regression that motivated this).
    if channel and not _slack_token and not os.environ.get(WORKERS_BOT_TOKEN_ENV):
        detail = "WORKERS_BOT_TOKEN not set; refusing to fall back to agent token (see #241)"
        logger.error("dispatch error reason=workers_token_missing detail=%r", detail)
        return {
            "status": "error",
            "reason": "workers_token_missing",
            "detail": detail,
        }

    dispatch_id = _new_dispatch_id(now)
    workspace = root / dispatch_id
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        workspace.chmod(0o700)
    except OSError:
        # Some volume backings (notably CI sandboxes) refuse chmod.
        pass

    # Initial state files (everything the supervisor needs except pid).
    _atomic_write(workspace / "started_at", now.isoformat())
    _atomic_write(workspace / "budget", str(int(budget_seconds)))
    _atomic_write(workspace / "channel", channel)
    _atomic_write(workspace / "thread_ts", thread_ts)
    _atomic_write(workspace / "agent", agent)
    _atomic_write(workspace / "issue_url", issue_url)
    _atomic_write(workspace / "model", model)
    _atomic_write(workspace / "persona", persona)

    # D-3: Seed per-dispatch auth directory from canonical creds. Skip
    # when exec_override is set (test / smoke-probe mode, no real claude).
    auth_dir: Path | None = None
    if exec_override is None:
        seed_fn = _seed_auth_fn if _seed_auth_fn is not None else _seed_auth_dir
        try:
            auth_dir = seed_fn(workspace)
        except RuntimeError as e:
            _atomic_write(workspace / "exitcode", "-1")
            detail = str(e)
            logger.error(
                "dispatch %s error reason=auth_seed_failed detail=%r",
                dispatch_id,
                detail,
            )
            _post_slack_message(
                channel,
                thread_ts,
                f"dispatch error — reason: auth_seed_failed\n```{_redact_detail(detail)}```",
                token=_slack_token,
                dispatch_id=dispatch_id,
                persona=persona,
            )
            return {
                "status": "error",
                "reason": "auth_seed_failed",
                "detail": detail,
                "dispatch_id": dispatch_id,
                "workspace": str(workspace),
            }

    # D-5: Short-circuit when quota soft-lock is active.
    if _QUOTA_AVAILABLE and _quota is not None:
        _cfg = _load_quota_config()
        _locked, _retry_after = _quota.is_locked(root, now, window_hours=_cfg["window_hours"])
        if _locked:
            _atomic_write(workspace / "exitcode", str(EXIT_LAUNCH_FAILED))
            detail = f"retry_after={_retry_after}"
            logger.error(
                "dispatch %s error reason=quota_locked detail=%r",
                dispatch_id,
                detail,
            )
            _post_slack_message(
                channel,
                thread_ts,
                f"dispatch error — reason: quota_locked\n```{_redact_detail(detail)}```",
                token=_slack_token,
                dispatch_id=dispatch_id,
                persona=persona,
            )
            return {
                "status": "error",
                "reason": "quota_locked",
                "retry_after": _retry_after,
                "dispatch_id": dispatch_id,
                "workspace": str(workspace),
            }

    # Issue #307: Clone origin/main into <workspace>/repo/ before acquiring
    # a slot so clone failures surface as structured errors without holding
    # a slot. Skipped when exec_override is set (test / smoke-probe mode).
    repo_path: "Path | None" = None
    if exec_override is None:
        clone_fn = _clone_repo_fn if _clone_repo_fn is not None else _clone_repo_into_workspace
        try:
            repo_path = clone_fn(workspace, issue_url)
        except RuntimeError as e:
            _atomic_write(workspace / "exitcode", str(EXIT_LAUNCH_FAILED))
            detail = str(e)
            logger.error(
                "dispatch %s error reason=clone_failed detail=%r",
                dispatch_id,
                detail,
            )
            _post_slack_message(
                channel,
                thread_ts,
                f"dispatch error — reason: clone_failed\n```{_redact_detail(detail)}```",
                token=_slack_token,
                dispatch_id=dispatch_id,
                persona=persona,
            )
            return {
                "status": "error",
                "reason": "clone_failed",
                "detail": detail,
                "dispatch_id": dispatch_id,
                "workspace": str(workspace),
            }

    # D-3: Acquire a slot from the N=3 pool (blocks if all busy).
    try:
        slot_idx, slot_num = _acquire_slot(
            root,
            dispatch_id,
            channel=channel,
            thread_ts=thread_ts,
            persona=persona,
            slack_token=_slack_token,
            _sleep_fn=_sleep_fn,
        )
    except RuntimeError as e:
        _atomic_write(workspace / "exitcode", str(EXIT_LAUNCH_FAILED))
        detail = str(e)
        logger.error(
            "dispatch %s error reason=not_in_channel detail=%r",
            dispatch_id,
            detail,
        )
        return {
            "status": "error",
            "reason": "not_in_channel",
            "detail": detail,
            "dispatch_id": dispatch_id,
            "workspace": str(workspace),
        }

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
    babysit_argv = (
        [sys.executable, babysit, "--dispatch-id", dispatch_id, "--cwd", str(workspace), "--slot-idx", str(slot_idx)]
        + ["--"]
        + child_cmd
    )

    # D-3: Set CLAUDE_CONFIG_DIR for the dispatched subprocess so it uses
    # the seeded copy, never the shared ~/.claude/.
    extra_env: dict[str, str] | None = None
    if auth_dir is not None:
        extra_env = {**os.environ, CANONICAL_CLAUDE_DIR_ENV: str(auth_dir)}

    # Issue #227: Inject dispatch identity — strip host PAT, write per-dispatch
    # .env / .gitconfig, and wire GH_TOKEN + GIT_CONFIG_GLOBAL for the worker.
    # Skipped when exec_override is set (test / smoke-probe mode).
    #
    # CAUTION: any future exec_override path that shells out to gh or git push
    # will inherit the host PAT from the parent environment because the strip
    # below is never reached.  Reviewers adding such a path should consider
    # whether to apply the strip there too.
    if exec_override is None:
        dispatch_token = _read_machine_user_token(_dispatch_token_path)
        if dispatch_token:
            # Machine-user token available: strip host PAT and inject dispatch identity.
            if extra_env is None:
                extra_env = dict(os.environ)
            extra_env.pop("GH_TOKEN", None)
            extra_env.pop("GITHUB_TOKEN", None)
            _seed_dispatch_identity(workspace, dispatch_token, dispatch_repo=repo_path)
            extra_env["GH_TOKEN"] = dispatch_token
            extra_env["GITHUB_TOKEN"] = dispatch_token
            extra_env["GIT_CONFIG_GLOBAL"] = str(workspace / ".gitconfig")
            logger.info(
                "dispatch %s: aidt-dispatch identity injected from %s",
                dispatch_id,
                _dispatch_token_path or DISPATCH_TOKEN_PATH,
            )
        else:
            # No machine-user token — let the worker inherit the host PAT.
            logger.warning(
                "dispatch %s: dispatch token unavailable; worker will inherit host GitHub credentials"
                " — machine-user identity not active",
                dispatch_id,
            )

    base_response: dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "workspace": str(workspace),
        "budget_seconds": int(budget_seconds),
        "model": model,
        "persona": persona,
        "supervision_mode": mode,
        "slot": slot_num,
    }
    if auth_dir is not None:
        base_response["auth_dir"] = str(auth_dir)

    if mode == SUPERVISION_MODE_INLINE:
        result = _launch_inline(
            popen=popen,
            babysit_argv=babysit_argv,
            workspace=workspace,
            base_response=base_response,
            extra_env=extra_env,
        )
        # Inline mode: babysit has exited (and released the slot in its
        # finally). Release here too — idempotent if babysit already did it.
        _release_slot(_slots_dir(root), slot_idx)
        return result

    result = _launch_poll(
        popen=popen,
        babysit_argv=babysit_argv,
        workspace=workspace,
        base_response=base_response,
        extra_env=extra_env,
    )
    # Poll mode: if spawn failed, no babysit will release the slot.
    if result.get("status") == "launch_failed":
        _release_slot(_slots_dir(root), slot_idx)
    # On success, the babysit releases the slot in its own finally block
    # (via --slot-idx arg) when the subprocess exits.
    return result


def _launch_poll(
    *,
    popen: Any,
    babysit_argv: list[str],
    workspace: Path,
    base_response: dict[str, Any],
    extra_env: dict[str, str] | None = None,
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
            env=extra_env,
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
    extra_env: dict[str, str] | None = None,
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
            env=extra_env,
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


def dispatch_status(*, workspace_root: Path | None = None) -> dict[str, Any]:
    """Return a snapshot of the slot pool and FIFO queue.

    Shape::

        {
          "running": ["dispatch-...", ...],  # dispatch IDs holding a slot
          "queued":  ["dispatch-...", ...],  # dispatch IDs in FIFO order
          "pool_size": 3,
          "slots_free": int,
        }

    ``running`` entries are ordered by slot index (0→2).
    ``queued`` entries are in FIFO order (front = next to run).
    """
    root = workspace_root if workspace_root is not None else _workspace_root()

    slots_dir = root / POOL_SLOTS_DIR_NAME
    queue_dir = root / POOL_QUEUE_DIR_NAME

    # Running: read dispatch IDs from slot lock files (slot-0 … slot-2).
    running: list[str] = []
    if slots_dir.exists():
        for slot_path in sorted(slots_dir.iterdir()):
            if slot_path.is_file() and slot_path.name.startswith("slot-"):
                try:
                    did = slot_path.read_text().strip()
                    if did:
                        running.append(did)
                except OSError:
                    pass

    # Queued: read dispatch IDs from ticket files in FIFO sort order.
    queued: list[str] = []
    if queue_dir.exists():
        for ticket in sorted(queue_dir.iterdir(), key=lambda p: p.name):
            if ticket.is_file():
                try:
                    did = ticket.read_text().strip()
                    if did:
                        queued.append(did)
                except OSError:
                    pass

    return {
        "running": running,
        "queued": queued,
        "pool_size": POOL_SIZE,
        "slots_free": POOL_SIZE - len(running),
    }


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
    # D-7: Internal flag set by the router when re-invoking after Approve.
    # Not part of the public verb contract — the router is a trusted caller.
    parser.add_argument(
        "--approved",
        dest="approved",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    return parser


def _build_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch.handler dispatch_status", add_help=False)
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

    # Release the slot after state writes (preserve state-before-cleanup
    # invariant) so the next queued dispatch can proceed without waiting
    # for the janitor.
    _release_slot_for_dispatch(_slots_dir(root), dispatch_id)

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

    # D-6: Run the janitor exactly once per process lifetime, lazily, on
    # the first non-health verb invocation so health checks are never blocked.
    _maybe_run_janitor()

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
            _approved=args.approved,
        )
        print(json.dumps(result))
        # ``launched`` (poll) and ``completed`` (inline) are both
        # successful outcomes for the CLI — exit 0. ``launch_failed``
        # and ``failed`` (nonzero exitcode in inline) are errors.
        # ``approval_required`` is also a non-error exit — the agent
        # should emit a draft-approval block based on the preview.
        ok_statuses = {"launched", "completed", "approval_required"}
        return EXIT_OK if result.get("status") in ok_statuses else EXIT_LAUNCH_FAILED

    if verb == "dispatch_status":
        _build_status_parser().parse_args(rest)
        print(json.dumps(dispatch_status()))
        return EXIT_OK

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

    if verb in ("schedule_wakeup", "schedule_wakeup_poll", "cancel_wakeup"):
        return _run_wakeup_verb(verb, rest)

    print(
        json.dumps(
            {
                "error": "unknown_verb",
                "verb": verb,
                "message": (
                    "Known verbs: dispatch_health, dispatch_issue, dispatch_status, dispatch_cancel,"
                    " schedule_wakeup, schedule_wakeup_poll, cancel_wakeup."
                ),
            }
        )
    )
    return EXIT_USAGE


def _run_wakeup_verb(verb: str, rest: list[str]) -> int:
    """Dispatch one of the three built-in wakeup verbs (#312)."""
    from router.scheduled_tasks.bootstrap import open_store  # noqa: PLC0415
    from router.scheduled_tasks.wakeup_verbs import (  # noqa: PLC0415
        cancel_wakeup,
        schedule_wakeup,
        schedule_wakeup_poll,
    )

    agent_name = os.environ.get(DISPATCH_AGENT_ENV, "").strip()
    if not agent_name:
        print(
            json.dumps(
                {
                    "error": "missing_agent_context",
                    "message": f"{DISPATCH_AGENT_ENV} not set — wakeup verbs require a dispatch context",
                }
            )
        )
        return EXIT_USAGE

    store = open_store(seed_defaults=False)
    try:
        if verb == "schedule_wakeup":
            parser = argparse.ArgumentParser(prog=f"dispatch.handler {verb}", add_help=False)
            parser.add_argument("--delay-seconds", type=int, required=True)
            parser.add_argument("--reason", required=True)
            args = parser.parse_args(rest)
            result = schedule_wakeup(store, agent_name, delay_seconds=args.delay_seconds, reason=args.reason)

        elif verb == "schedule_wakeup_poll":
            parser = argparse.ArgumentParser(prog=f"dispatch.handler {verb}", add_help=False)
            parser.add_argument("--period-seconds", type=int, required=True)
            parser.add_argument("--max-attempts", type=int, required=True)
            parser.add_argument("--reason", required=True)
            args = parser.parse_args(rest)
            try:
                result = schedule_wakeup_poll(
                    store,
                    agent_name,
                    period_seconds=args.period_seconds,
                    max_attempts=args.max_attempts,
                    reason=args.reason,
                )
            except ValueError as exc:
                result = {"error": "invalid_args", "message": str(exc)}

        else:  # cancel_wakeup
            parser = argparse.ArgumentParser(prog=f"dispatch.handler {verb}", add_help=False)
            parser.add_argument("--task-id", required=True)
            args = parser.parse_args(rest)
            result = cancel_wakeup(store, agent_name, task_id=args.task_id)

    except ValueError as exc:
        result = {"error": "ceiling_exceeded", "message": str(exc)}
    finally:
        store.close()

    print(json.dumps(result))
    return EXIT_OK if "error" not in result else EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
