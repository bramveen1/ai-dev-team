"""CLI entry point for the browser_use pack.

The agent invokes this script from Bash:

    python /config/packs/browser_use/handler.py <action> \\
        --profile <profile-name> < payload.json

What it does:

1. Validate the action against the pack's manifest. Write verbs
   declared in ``pack.yaml``'s ``approve:`` list are refused here —
   the agent must emit a ``draft-approval`` block instead of calling
   the handler directly for those.
2. Validate the profile name shape (regex only — no filesystem
   access). Profile directory creation, mode checking, and the keyed
   secret bundle all live in the sidecar, which owns the on-disk
   profile tree at mode 0700.
3. Verify the sidecar is reachable (``GET /health``). If not, exit
   with code ``EXIT_SIDECAR_UNREACHABLE`` and an actionable hint that
   tells the operator how to start it.
4. POST the action + payload (including ``create_missing``) to the
   sidecar at ``/api/<action>``.
5. Print the sidecar's JSON response on stdout.

Secret handling and profile-dir ownership both live in the **sidecar**,
not here. The host age keyfile and the per-profile cookie dirs are
mounted only into the sidecar container (see ``docker-compose.yml``);
the agent container has no access to either. That's the security
boundary called out in the pack README's threat model. As a result
this handler reads no secrets, decrypts nothing, never touches the
profile dir, and never stats or mkdirs anything under
``/config/browser_profiles``.

Errors are written to stderr. The agent reads stdout + the exit code.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# When invoked as ``python /config/packs/browser_use/handler.py`` the
# pack's own directory is not on ``sys.path``. Add it so the relative
# ``helpers`` package resolves the same way regardless of invocation
# (script vs. ``importlib.util.spec_from_file_location`` in tests).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from helpers.profile_name import (  # noqa: E402  — path mutation above
    ProfileNameError,
    validate_name,
)
from helpers.sidecar_client import (  # noqa: E402
    SidecarBadResponse,
    SidecarClient,
    SidecarUnreachable,
)

logger = logging.getLogger("browser_use.handler")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_SIDECAR_UNREACHABLE = 2
EXIT_PROFILE_ERROR = 4
EXIT_BAD_RESPONSE = 5

# Actions that need no profile name — ``health`` is a sidecar probe,
# nothing more. Everything else operates on a named profile.
_PROFILE_LESS_ACTIONS = frozenset({"health"})

# Known browser-driving verbs. The handler refuses unknown actions
# rather than blindly forwarding them — the sidecar API surface is
# pinned and the agent shouldn't be sending novel verbs.
_KNOWN_READ_ACTIONS = frozenset({"navigate", "extract", "screenshot", "health"})


@dataclass
class _Args:
    action: str
    profile: str | None
    timeout: float | None
    payload_path: str | None
    no_create_profile: bool


def _parse_args(argv: list[str]) -> _Args:
    parser = argparse.ArgumentParser(prog="browser_use.handler", description=__doc__.splitlines()[0])
    parser.add_argument("action", help="action verb (navigate, extract, screenshot, health, …)")
    parser.add_argument("--profile", help="profile name (required except for `health`)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="override per-request timeout in seconds",
    )
    parser.add_argument(
        "--payload",
        dest="payload_path",
        default=None,
        help="path to a JSON file with the action payload; defaults to stdin",
    )
    parser.add_argument(
        "--no-create-profile",
        action="store_true",
        help="refuse to create a missing profile dir (use for read-only actions)",
    )
    ns = parser.parse_args(argv)
    return _Args(
        action=ns.action,
        profile=ns.profile,
        timeout=ns.timeout,
        payload_path=ns.payload_path,
        no_create_profile=ns.no_create_profile,
    )


def _read_payload(path: str | None) -> dict[str, Any]:
    if path is None:
        # When stdin isn't a TTY-or-pipe we'd block forever waiting for
        # input; check ``isatty`` and bail out with an empty payload.
        # Useful for actions like ``health`` that need no args, and for
        # tests where pytest captures stdin.
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        try:
            raw = sys.stdin.read()
        except OSError:
            # pytest's captured-stdin raises OSError on read; treat that
            # as "no payload supplied" rather than a hard failure.
            return {}
    else:
        with open(path) as f:
            raw = f.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"payload is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"payload must be a JSON object (got {type(data).__name__})")
    return data


def _load_approve_list(pack_yaml: Path | None = None) -> frozenset[str]:
    """Return the set of approval-gated verbs declared by the pack manifest.

    Read directly from ``pack.yaml`` so the handler and the manifest
    can never drift — there's exactly one source of truth for which
    actions need a ``draft-approval`` block.
    """
    path = pack_yaml if pack_yaml is not None else (_HERE / "pack.yaml")
    if not path.exists():
        logger.warning("pack.yaml not found at %s; treating all actions as read-only", path)
        return frozenset()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    approve = data.get("approve") or []
    if not isinstance(approve, list):
        logger.warning("pack.yaml 'approve' is not a list; treating all actions as read-only")
        return frozenset()
    return frozenset(str(v) for v in approve)


def run(argv: list[str] | None = None) -> int:
    """Run the handler. Returns the exit code. Public so tests can drive it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    write_actions = _load_approve_list()
    if args.action in write_actions:
        print(
            json.dumps(
                {
                    "error": "approval_required",
                    "message": (
                        f"action {args.action!r} is approval-gated. "
                        "Emit a draft-approval block instead of calling the handler."
                    ),
                }
            )
        )
        return EXIT_USAGE

    if args.action not in _KNOWN_READ_ACTIONS:
        print(json.dumps({"error": "unknown_action", "action": args.action}))
        return EXIT_USAGE

    # Validate the profile name shape — pure regex, no filesystem.
    # The sidecar (which owns the on-disk profile tree at mode 0700)
    # does the actual resolve / mkdir / mode-drift check after the
    # request lands. Skip for ``health`` — it doesn't need a profile.
    profile_name: str | None = None
    if args.action not in _PROFILE_LESS_ACTIONS:
        if not args.profile:
            print(json.dumps({"error": "missing_profile", "message": "--profile is required"}))
            return EXIT_USAGE
        try:
            profile_name = validate_name(args.profile)
        except ProfileNameError as e:
            sys.stderr.write(f"browser_use: {e}\n")
            return EXIT_PROFILE_ERROR

    # Build the payload sent to the sidecar. The handler does not
    # decrypt secrets and does not touch the profile dir — both belong
    # to the sidecar (it owns the keyfile and the mode-0700 cookie
    # dirs). We just forward the profile name, the caller payload, and
    # the ``create_missing`` flag so the sidecar can honour
    # ``--no-create-profile`` server-side.
    try:
        payload = _read_payload(args.payload_path)
    except ValueError as e:
        sys.stderr.write(f"browser_use: {e}\n")
        return EXIT_USAGE
    if profile_name is not None:
        payload["profile"] = profile_name
        payload["create_missing"] = not args.no_create_profile

    # Talk to the sidecar.
    try:
        with SidecarClient(timeout=args.timeout) as client:
            if args.action == "health":
                response = client.health()
            else:
                response = client.invoke(args.action, payload)
    except SidecarUnreachable as e:
        sys.stderr.write(f"browser_use: sidecar unreachable — {e}\n")
        return EXIT_SIDECAR_UNREACHABLE
    except SidecarBadResponse as e:
        sys.stderr.write(f"browser_use: {e}\n")
        return EXIT_BAD_RESPONSE

    print(json.dumps(response.body))
    return EXIT_OK if 200 <= response.status < 300 else EXIT_BAD_RESPONSE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
