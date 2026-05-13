"""FastAPI app for the browser_use sidecar.

Exposes the tiny JSON-over-HTTP surface the pack handler talks to:

- ``GET /health`` — readiness probe. Verifies the keyfile is present
  and not world-readable, and reports the age binary version. The
  sidecar refuses to come up at all if the keyfile mount is missing
  or unsafe — that's the boundary check called out in the issue's
  acceptance criteria.
- ``POST /api/<action>`` — run one browser action against the named
  profile. The sidecar resolves the profile dir, decrypts any
  ``<profile>.env.age`` bundle in memory using the mounted keyfile,
  and dispatches to the action handler. Decrypted values are scrubbed
  out of the response body before it leaves the process.

Real Chromium-driven work lives in
``browser_use_sidecar.agent_runner.run_agent`` — keeping it in a
separate module gives unit tests a single seam to mock instead of
pulling Playwright + Chromium onto the test runner. The action
handlers in this file just validate the request shape, delegate to
the runner, and scrub the response on the way back.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Any

# The sidecar runs with ``PYTHONPATH=/opt/pack``; that puts the pack
# directory (``packs/browser_use``) on the path so we can import the
# shared ``helpers`` package the handler also uses. Keeping helpers in
# one place means the decryption + profile validation + scrub logic is
# tested once and used in both contexts.
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

# Imports below intentionally use the pack-local ``helpers`` package;
# tests insert the pack root onto sys.path before importing the server.
from helpers.profile_manager import (  # noqa: E402  — pack-local import
    Profile,
    ProfileError,
    ensure_profile,
    resolve_profiles_dir,
)
from helpers.secrets import (  # noqa: E402
    SecretBundle,
    SecretError,
    assert_keyfile_safe,
    load_bundle,
    resolve_bundle_dir,
    resolve_keyfile,
)

from browser_use_sidecar.agent_runner import (  # noqa: E402
    MalformedProfileError,
    run_agent,
)

logger = logging.getLogger("browser_use.sidecar")

_SIDECAR_VERSION = "0.1.0"


def _age_version() -> str:
    binary = shutil.which("age")
    if not binary:
        return "absent"
    try:
        out = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return out.stdout.decode("utf-8", errors="replace").strip() or "unknown"


_KNOWN_ACTIONS = frozenset({"navigate", "extract", "screenshot"})


async def _dispatch_action(
    action: str,
    profile: Profile,
    bundle: SecretBundle,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run one browser action against the named profile.

    Hands the work off to :func:`agent_runner.run_agent`, which owns
    the Browser Use bindings + the open/close lifecycle. This function
    handles the HTTP-layer error mapping:

    - Unknown verb / bad payload → 400 (caller error).
    - Malformed profile state (cookies.json not JSON, …) → 400, no
      browser spawned. The Agent would either silently lose the
      session or crash mid-run with a confusing message — refuse it
      up front instead.
    - Agent itself ran and reported an error → 200 with
      ``status="error"`` so the handler still gets a structured body
      to relay to the agent rather than a 5xx + stack trace.
    """
    if action not in _KNOWN_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")

    try:
        return await run_agent(action=action, profile=profile, bundle=bundle, payload=payload)
    except MalformedProfileError as e:
        # Scrub the message because the profile path is reflected back
        # in the error and could in principle include a profile name
        # that overlaps a secret value (unlikely, but the bundle is
        # already on hand — cheap to apply).
        message = bundle.scrub(str(e)) if bundle.values else str(e)
        raise HTTPException(status_code=400, detail=f"malformed profile: {message}") from e
    except ValueError as e:
        # ``build_task`` raises ValueError for payload shape issues.
        raise HTTPException(status_code=400, detail=str(e)) from e


def _build_app() -> FastAPI:
    app = FastAPI(title="browser-use sidecar", version=_SIDECAR_VERSION)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Readiness probe — the handler's first call.

        Returns 200 only when the keyfile is mounted and safe; that's
        the contract the handler relies on for "is the sidecar real?".
        """
        keyfile = resolve_keyfile()
        try:
            assert_keyfile_safe(keyfile)
        except SecretError as e:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "reason": str(e),
                    "keyfile": str(keyfile),
                },
            )
        return {
            "status": "ok",
            "version": _SIDECAR_VERSION,
            "age_version": _age_version(),
            "keyfile": str(keyfile),
            "profiles_dir": str(resolve_profiles_dir()),
            "bundle_dir": str(resolve_bundle_dir()),
        }

    @app.post("/api/{action}")
    async def run_action(action: str, body: dict[str, Any]) -> dict[str, Any]:
        profile_name = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(profile_name, str) or not profile_name:
            raise HTTPException(status_code=400, detail="payload missing 'profile' string")

        # Keyfile gate — same check as /health, run again so a request
        # that arrived during a keyfile rotation gets a clean 503 rather
        # than a confusing crash deeper in.
        try:
            assert_keyfile_safe(resolve_keyfile())
        except SecretError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        try:
            profile = ensure_profile(profile_name)
        except ProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        try:
            bundle = load_bundle(profile.name)
        except SecretError as e:
            # Specific HTTP code (502) because the operator's bundle is
            # broken — distinct from a 503 (keyfile missing) or a 400
            # (caller error). The handler surfaces this as EXIT_BAD_RESPONSE.
            raise HTTPException(status_code=502, detail=f"bundle decrypt failed: {e}") from e

        result = await _dispatch_action(action, profile, bundle, body)
        # Scrub any echoed plaintext on the way out. Walks the
        # response one level deep — most fields are scalars; the
        # ``extracted`` / ``screenshots`` lists get walked element by
        # element so an Agent that echoed a secret into extracted page
        # text still gets caught. This catches a buggy action handler
        # that accidentally returns env values in its response body.
        # It cannot defeat semantic leaks (the secret encoded
        # differently or split across keys); the caller and the action
        # handlers must avoid those at the source. README's "Log
        # scrubbing" section spells this out.
        return _scrub_response(result, bundle)

    return app


def _scrub_response(result: dict[str, Any], bundle: SecretBundle) -> dict[str, Any]:
    """Walk the response tree and run ``bundle.scrub`` on every string.

    Only descends into ``dict`` and ``list`` containers — sufficient
    for the runner's response shape (string fields plus an ``extracted``
    list of strings and a ``screenshots`` list of paths). Anything
    deeper is treated as opaque and passed through. The action
    handlers must not return arbitrary nested DOM blobs anyway; that's
    the "treat scrub as a second line of defence" rule from the README.
    """

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return bundle.scrub(value)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(result)


# At import time we defer the keyfile check so uvicorn / pytest can
# still import the module in environments without a real keyfile (the
# tests stub the env var to a controlled path). The check fires on the
# first /health or /api request.

app = _build_app()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — CLI entry
    """Stand-alone runner for ad-hoc starts (``python -m browser_use_sidecar.server``).

    Production uses ``uvicorn browser_use_sidecar.server:app`` from
    the Dockerfile CMD; this entry point exists only to keep
    ``python -m`` working for local debugging.
    """
    import uvicorn

    host = os.environ.get("BROWSER_USE_SIDECAR_HOST", "0.0.0.0")
    port = int(os.environ.get("BROWSER_USE_SIDECAR_PORT", "8080"))
    logger.info("starting browser-use sidecar on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
