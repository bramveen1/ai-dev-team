"""CLI entry point for the browser_use pack.

The agent invokes this script from Bash:

    python /config/packs/browser_use/handler.py <action> \\
        --profile <profile-name> < payload.json

What it does:

1. Verify the sidecar is reachable. If not, exit 2 with a hint that
   tells the operator how to start it. This is the "dispatcher rejects
   invocation if the sidecar service is unreachable" acceptance
   criterion — enforced at the call boundary rather than buried in the
   handler.
2. Verify the host age keyfile is present and not world-readable
   (delegated to ``helpers/secrets.assert_keyfile_safe``).
3. Resolve / create the profile dir at mode 0700.
4. Decrypt the per-profile secrets bundle in memory.
5. POST the action + payload to the sidecar with the secrets injected
   under ``payload["env"]`` so the sidecar can supply them to Browser
   Use at session-start time.
6. Print the sidecar's JSON response on stdout, with any decrypted
   secret values scrubbed first.

Errors are logged to stderr (scrubbed) and the process exits non-zero.
The agent reads stdout + the exit code; secret values never appear in
either.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# When invoked as ``python /config/packs/browser_use/handler.py`` the
# pack's own directory is not on ``sys.path``. Add it so the relative
# ``helpers`` package resolves the same way regardless of invocation
# (script vs. ``importlib.util.spec_from_file_location`` in tests).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from helpers.profile_manager import (  # noqa: E402  — path mutation above
    Profile,
    ProfileError,
    ensure_profile,
)
from helpers.secrets import (  # noqa: E402
    SecretBundle,
    SecretError,
    assert_keyfile_safe,
    load_bundle,
    resolve_keyfile,
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
EXIT_SECRET_ERROR = 3
EXIT_PROFILE_ERROR = 4
EXIT_BAD_RESPONSE = 5

# Read actions bypass approval. Write actions are gated upstream by the
# pack manifest's `approve:` list; if we see one here the agent skipped
# the draft-approval flow and we refuse.
_READ_ACTIONS = frozenset({"navigate", "extract", "screenshot", "health"})
_WRITE_ACTIONS = frozenset({"submit", "post", "apply", "purchase"})


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


def _install_scrub_filter(bundle: SecretBundle) -> None:
    """Install a logging filter that redacts secret values before emission.

    Belt-and-braces: we already scrub the JSON response, but a stray
    ``logger.debug("token=%s", token)`` would still leak. The filter
    rewrites every record's formatted message in place.

    The filter is attached to every handler on the root logger (not to
    the root logger itself) because handlers run their filters on
    propagated records — a filter attached to the root logger object
    only fires for records logged directly through ``root.handle()``.
    """

    class _ScrubFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            message = record.getMessage()
            scrubbed = bundle.scrub(message)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = None
            return True

    scrub = _ScrubFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(scrub)
    # Also attach to root itself, so direct ``root.log(...)`` calls and
    # tests that capture via the root logger's filter chain still see
    # the scrubbed text.
    root.addFilter(scrub)


def run(argv: list[str] | None = None) -> int:
    """Run the handler. Returns the exit code. Public so tests can drive it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.action in _WRITE_ACTIONS:
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

    if args.action not in _READ_ACTIONS:
        print(json.dumps({"error": "unknown_action", "action": args.action}))
        return EXIT_USAGE

    # Secret keyfile precheck — refuse to start the session if the
    # keyfile is missing or world-readable. We always do this even for
    # actions that need no secrets, because the operator's first sign
    # that something is wrong should be "your keyfile is broken", not
    # "the agent silently never reached the sidecar with creds".
    keyfile = resolve_keyfile()
    try:
        assert_keyfile_safe(keyfile)
    except SecretError as e:
        sys.stderr.write(f"browser_use: {e}\n")
        return EXIT_SECRET_ERROR

    # Resolve profile dir up front (skip for `health` — it doesn't need one).
    profile: Profile | None = None
    if args.action != "health":
        if not args.profile:
            print(json.dumps({"error": "missing_profile", "message": "--profile is required"}))
            return EXIT_USAGE
        try:
            profile = ensure_profile(
                args.profile,
                create_missing=not args.no_create_profile,
            )
        except ProfileError as e:
            sys.stderr.write(f"browser_use: {e}\n")
            return EXIT_PROFILE_ERROR

    # Decrypt secrets in-memory (empty bundle is fine — many profiles
    # carry no env-injected secrets, only cookies in the profile dir).
    bundle = SecretBundle()
    if profile is not None:
        try:
            bundle = load_bundle(profile.name)
        except SecretError as e:
            sys.stderr.write(f"browser_use: {e}\n")
            return EXIT_SECRET_ERROR
    _install_scrub_filter(bundle)

    # Build the payload sent to the sidecar.
    try:
        payload = _read_payload(args.payload_path)
    except ValueError as e:
        sys.stderr.write(f"browser_use: {e}\n")
        return EXIT_USAGE
    if profile is not None:
        payload["profile"] = profile.name
        payload["profile_path"] = str(profile.path)
    if bundle.values:
        payload["env"] = dict(bundle.values)

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
        sys.stderr.write(f"browser_use: {bundle.scrub(str(e))}\n")
        return EXIT_BAD_RESPONSE

    # Scrub any secret values that the sidecar happened to echo back
    # before printing to stdout — the agent must never see them.
    body_json = bundle.scrub(json.dumps(response.body))
    print(body_json)
    return EXIT_OK if 200 <= response.status < 300 else EXIT_BAD_RESPONSE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
