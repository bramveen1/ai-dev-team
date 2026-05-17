"""CLI entry point for the dispatch pack.

The agent invokes this script from Bash:

    python /config/packs/dispatch/handler.py <verb> [--<arg> ...]

Verbs planned for the v1 dispatch pack:

- ``dispatch_health``   — smoke probe (this scaffold).
- ``dispatch_issue``    — primary verb, runs a headless claude session.
- ``dispatch_status``   — read latest event from an in-flight dispatch.
- ``dispatch_cancel``   — SIGTERM the process group, tear down workspace.

Only ``dispatch_health`` is wired up in this scaffold (D-1). The other
three verbs land in D-2 / D-4. See ``docs/design/dispatch-pack.md`` and
the README in this directory.

``dispatch_health`` returns four fields the operator cares about:

- ``cli_version``               — output of ``claude --version`` (str).
- ``claude_path``               — absolute path to the resolved binary.
- ``workspace_volume_writable`` — does a touch under ``/var/lib/dispatch/``
                                  succeed?
- ``sonnet_probe_ok``           — does a 30s ``claude -p`` against Sonnet
                                  round-trip cleanly?

The probe is Sonnet-pinned on purpose: health checks fire on every Sam
container start and we'd rather not burn the Opus 5h quota on liveness.

Failures are surfaced as ``false`` in the relevant field plus a
structured detail (CLI exit code, error message) — they MUST NOT raise
through to a 500. The acceptance criteria spell that out.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("dispatch.handler")

EXIT_OK = 0
EXIT_USAGE = 1

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


def _workspace_root() -> Path:
    return Path(os.environ.get(WORKSPACE_ROOT_ENV, DEFAULT_WORKSPACE_ROOT))


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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dispatch.handler", description=__doc__.splitlines()[0])
    parser.add_argument(
        "verb",
        help="dispatch verb (only `dispatch_health` is wired in the D-1 scaffold)",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    """Run the handler. Returns the exit code. Public so tests can drive it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.verb == "dispatch_health":
        print(json.dumps(dispatch_health()))
        return EXIT_OK

    print(
        json.dumps(
            {
                "error": "unknown_verb",
                "verb": args.verb,
                "message": (
                    "Only `dispatch_health` is implemented in the D-1 scaffold. "
                    "`dispatch_issue`, `dispatch_status`, and `dispatch_cancel` land in D-2 / D-4."
                ),
            }
        )
    )
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
