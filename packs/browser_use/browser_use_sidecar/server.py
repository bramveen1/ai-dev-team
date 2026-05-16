"""FastAPI app for the browser_use sidecar.

Exposes the tiny JSON-over-HTTP surface the pack handler talks to:

- ``GET /health`` — readiness probe. Verifies the keyfile is present
  and not world-readable, and reports the age binary version. The
  sidecar refuses to come up at all if the keyfile mount is missing
  or unsafe — that's the boundary check called out in the issue's
  acceptance criteria.
- ``POST /api/<action>`` — run one browser action against the named
  profile. The sidecar **owns** profile resolution: it validates the
  name, mkdirs the mode-0700 profile dir if missing (or refuses when
  the caller sent ``create_missing: false``), refuses to use a dir
  whose mode drifted, decrypts any ``<profile>.env.age`` bundle in
  memory using the mounted keyfile, and dispatches to the action
  handler. The pack handler in the agent container never touches the
  profile tree (it can't — different UID, mode 0700). Decrypted
  values are scrubbed out of the response body before it leaves the
  process.

Real Chromium-driven work lives in
``browser_use_sidecar.playwright_runner.run_verb`` — a thin wrapper
over Playwright with no LLM, no agent loop, no Anthropic API key
required. The handlers in this file just validate the request shape,
delegate to the runner, and scrub the response on the way back.
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
from helpers.credentials import (  # noqa: E402  — pack-local import
    CredentialError,
    remove_credential,
    set_credential,
)
from helpers.profile_manager import (  # noqa: E402
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

from browser_use_sidecar.playwright_runner import (  # noqa: E402
    LoginNotEnabledError,
    MalformedProfileError,
    VerbRunError,
    known_verbs,
    run_verb,
)

logger = logging.getLogger("browser_use.sidecar")

_SIDECAR_VERSION = "0.1.0"

# Which verbs need decrypted creds injected at session start. Read
# verbs (``navigate`` / ``extract`` / ``screenshot``) operate without
# credentials — the operator can use them on a profile that has no
# encrypted bundle, and a perms regression on
# ``/config/secrets/browser`` must not take down navigate. Write verbs
# (``submit`` / ``apply`` / ``post`` / ``purchase``) need creds to do
# anything useful. Unknown verbs default to True (fail-closed): a new
# verb that forgets to register here will load the bundle and surface
# any perm/decrypt failure cleanly rather than silently skipping it.
_VERB_NEEDS_SECRETS: dict[str, bool] = {
    "navigate": False,
    "extract": False,
    "screenshot": False,
    "submit": True,
    "apply": True,
    "post": True,
    "purchase": True,
    # ``login`` and ``session_status`` (issue #147) read credentials
    # from ``credentials.age`` directly inside the runner rather than
    # the ``<profile>.env.age`` env-style bundle, so they don't need
    # the env bundle loaded. Mark them ``False`` so a perms regression
    # on ``/config/secrets/browser`` doesn't take them down.
    "login": False,
    "session_status": False,
}


def _verb_needs_secrets(action: str) -> bool:
    return _VERB_NEEDS_SECRETS.get(action, True)


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


async def _dispatch_action(
    action: str,
    profile: Profile,
    bundle: SecretBundle,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run one browser action against the named profile.

    Hands the work off to :func:`playwright_runner.run_verb`, which
    owns the Chromium lifecycle. This function handles the HTTP-layer
    error mapping:

    - Unknown verb → 400 (caller error).
    - Bad payload (missing url, malformed selectors) → 400.
    - Malformed profile state (cookies.json not JSON, …) → 400, no
      browser spawned. Playwright would either silently lose the
      session or crash mid-run with a confusing message — refuse it
      up front instead.
    - Playwright failure (timeout, DNS) → 200 with ``status="error"``
      so the handler still gets a structured body to relay to the
      agent rather than a 5xx + stack trace.
    """
    if action not in known_verbs():
        raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")

    try:
        return await run_verb(verb=action, profile=profile, bundle=bundle, payload=payload)
    except MalformedProfileError as e:
        # Scrub the message because the profile path is reflected back
        # in the error and could in principle overlap a secret value.
        message = bundle.scrub(str(e)) if bundle.values else str(e)
        raise HTTPException(status_code=400, detail=f"malformed profile: {message}") from e
    except LoginNotEnabledError as e:
        # Profile hasn't opted into login. 400 with a structured shape
        # so the agent can recognise the case (vs. a generic VerbRunError).
        raise HTTPException(status_code=400, detail=f"login not enabled: {e}") from e
    except VerbRunError as e:
        # Payload-shape bug from the caller — surface as 400.
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

    # Issue #147 — credential management endpoints. These are
    # registered **before** the ``/api/{action}`` catch-all so the
    # explicit paths win. (FastAPI / Starlette match routes in
    # registration order; declaring an explicit ``/api/foo`` after a
    # catch-all ``/api/{x}`` would never match.)

    @app.post("/api/add_credential")
    async def add_credential(body: dict[str, Any]) -> Any:
        """Store one credential in the profile's ``credentials.age`` blob.

        **No-log policy** (issue #147 decision #3): the request body
        contains plaintext username/password received over the
        internal docker-network HTTP channel from the router's grant
        flow. We must never log the body, never echo any field of it
        back, and never write any of it to disk in plaintext. The
        confirmation response carries the ``credential_key`` only —
        nothing else.

        Failure responses: 400 for shape errors, 503 for keyfile
        problems, 500 for encryption failures. Even error bodies
        contain only the credential_key (when known) — never the
        plaintext.
        """
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        profile_name = body.get("profile")
        credential_key = body.get("credential_key")
        username = body.get("username")
        password = body.get("password")
        for field_name, value in (
            ("profile", profile_name),
            ("credential_key", credential_key),
            ("username", username),
            ("password", password),
        ):
            if not isinstance(value, str) or not value:
                raise HTTPException(
                    status_code=400,
                    detail=f"payload missing or non-string {field_name!r}",
                )

        try:
            assert_keyfile_safe(resolve_keyfile())
        except SecretError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        try:
            profile = ensure_profile(profile_name, create_missing=True)
        except ProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        try:
            set_credential(profile.path, credential_key, username, password)
        except CredentialError as e:
            # ``CredentialError.__str__`` is built from the underlying
            # age failure (a path or stderr line), never the plaintext
            # we passed in. Safe to surface.
            raise HTTPException(status_code=500, detail=f"credential store failed: {e}") from e

        return {
            "status": "ok",
            "action": "add_credential",
            "profile": profile.name,
            "credential_key": credential_key,
        }

    @app.post("/api/revoke_credential")
    async def revoke_credential_endpoint(body: dict[str, Any]) -> Any:
        """Remove one credential from the profile's ``credentials.age``.

        Other keys in the same file are preserved (acceptance
        criterion: ``revoke`` removes the named key but leaves other
        credentials in the file intact).
        """
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        profile_name = body.get("profile")
        credential_key = body.get("credential_key")
        for field_name, value in (("profile", profile_name), ("credential_key", credential_key)):
            if not isinstance(value, str) or not value:
                raise HTTPException(
                    status_code=400,
                    detail=f"payload missing or non-string {field_name!r}",
                )

        try:
            assert_keyfile_safe(resolve_keyfile())
        except SecretError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        try:
            profile = ensure_profile(profile_name, create_missing=False)
        except ProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        try:
            removed = remove_credential(profile.path, credential_key)
        except CredentialError as e:
            raise HTTPException(status_code=500, detail=f"credential revoke failed: {e}") from e

        return {
            "status": "ok",
            "action": "revoke_credential",
            "profile": profile.name,
            "credential_key": credential_key,
            "removed": removed,
        }

    @app.post("/api/{action}")
    async def run_action(action: str, body: dict[str, Any]) -> Any:
        # Captured outside the try so the catch-all envelope at the end
        # can include the profile name (when we got far enough to know
        # it) and scrub through whatever bundle we managed to load.
        profile_name_for_envelope: str | None = None
        bundle_for_envelope: SecretBundle = SecretBundle()

        try:
            profile_name = body.get("profile") if isinstance(body, dict) else None
            if not isinstance(profile_name, str) or not profile_name:
                raise HTTPException(status_code=400, detail="payload missing 'profile' string")
            profile_name_for_envelope = profile_name

            # Optional ``create_missing`` flag — defaults to True for
            # backwards compatibility, but the handler sends ``False`` when
            # the operator passed ``--no-create-profile`` (read-only mode).
            create_missing = body.get("create_missing", True) if isinstance(body, dict) else True
            if not isinstance(create_missing, bool):
                raise HTTPException(status_code=400, detail="payload 'create_missing' must be a boolean")

            # Keyfile gate — same check as /health, run again so a request
            # that arrived during a keyfile rotation gets a clean 503 rather
            # than a confusing crash deeper in.
            try:
                assert_keyfile_safe(resolve_keyfile())
            except SecretError as e:
                raise HTTPException(status_code=503, detail=str(e)) from e

            try:
                profile = ensure_profile(profile_name, create_missing=create_missing)
            except ProfileError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            # Read-only verbs (navigate / extract / screenshot) skip
            # ``load_bundle`` entirely. A perms regression on the
            # secrets dir — uid mismatch on a fresh bind-mount, ``:ro``
            # mount preventing the entrypoint chown — must not take
            # down a verb that doesn't actually need credentials. Write
            # verbs (submit / apply / post / purchase) still load the
            # bundle; unknown verbs fail-closed via
            # ``_verb_needs_secrets`` and load it too.
            if _verb_needs_secrets(action):
                try:
                    bundle = load_bundle(profile.name)
                except SecretError as e:
                    # Specific HTTP code (502) because the operator's bundle is
                    # broken — distinct from a 503 (keyfile missing) or a 400
                    # (caller error). The handler surfaces this as EXIT_BAD_RESPONSE.
                    raise HTTPException(status_code=502, detail=f"bundle decrypt failed: {e}") from e
            else:
                bundle = SecretBundle()
            bundle_for_envelope = bundle

            result = await _dispatch_action(action, profile, bundle, body)
            # Scrub any echoed plaintext on the way out. Walks one level
            # deep (dicts + lists + nested dict values) so an ``extracted``
            # field or a screenshot path that accidentally contains a
            # secret value still gets caught. It cannot defeat semantic
            # leaks (encoded values, partial strings); the verb
            # implementations must avoid those at the source. The README's
            # "Log scrubbing" section spells this out.
            return _scrub_response(result, bundle)
        except HTTPException:
            # FastAPI's own error path — let it propagate as-is so the
            # 400/502/503 status codes the rest of this handler raises
            # still reach the client unchanged.
            raise
        except Exception as e:
            # PR #142 introduced the ``{"status": "error", ...}`` envelope
            # for failures *inside* ``run_verb``; that catch can't see
            # anything raised before dispatch (e.g. ``load_bundle`` hitting
            # a ``PermissionError`` from a uid-mismatched bind mount, see
            # issue #143). This outer except extends the same contract to
            # the rest of ``run_action`` — log the traceback, scrub the
            # message through whatever bundle we managed to load, and
            # return the structured envelope with a 500 status. The
            # handler distinguishes via the envelope shape, not the code.
            logger.error(
                "run_action %s failed with %s",
                action,
                type(e).__name__,
                exc_info=True,
            )
            message = bundle_for_envelope.scrub(str(e)) if bundle_for_envelope.values else str(e)
            return JSONResponse(
                status_code=500,
                content={
                    "status": "error",
                    "action": action,
                    "profile": profile_name_for_envelope,
                    "error": message,
                    "error_type": type(e).__name__,
                },
            )

    return app


def _scrub_response(result: dict[str, Any], bundle: SecretBundle) -> dict[str, Any]:
    """Walk the response tree and run ``bundle.scrub`` on every string.

    Only descends into ``dict`` and ``list`` containers — sufficient
    for the runner's response shape (string fields plus an
    ``extracted`` dict of selector → text and a ``screenshot_b64``
    field). Anything deeper is treated as opaque and passed through.
    The verbs must not return arbitrary nested DOM blobs anyway;
    that's the "treat scrub as a second line of defence" rule.
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
