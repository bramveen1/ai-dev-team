"""Run one Playwright verb against a per-profile browser context.

This module is the single seam between the FastAPI app (``server.py``)
and Playwright. Three responsibilities live here:

1. **Map the pack's declared verbs to deterministic Playwright calls.**
   No LLM, no agent loop, no API key. ``navigate`` is ``page.goto``,
   ``extract`` is a per-selector ``locator.text_content`` walk,
   ``screenshot`` is ``page.screenshot``. The earlier draft of this
   module (PR #130) routed everything through the Browser Use agent
   loop, which is LLM-driven and required a separately-billed
   Anthropic API key. Issue #119 re-scoped to direct Playwright after
   that surfaced on a real session.

2. **Own the browser/context/page lifecycle.** Open in the order
   browser → context → page, close in reverse — every step in a
   ``finally`` so a crash mid-run never leaks a Chromium process.

3. **Wire the decrypted profile into the context.** Storage state from
   ``<profile>/storage_state.json`` (cookies + localStorage) is loaded
   into the new context before any verb runs; on success the updated
   storage is written back so the session sticks across requests.
   Profile state is validated *before* any browser is spawned — a
   malformed ``cookies.json`` / ``storage_state.json`` trips a
   fast-fail.

Issue #147 added two new verbs:

- ``login`` — submit a username/password form and persist the
  resulting authenticated session. Credentials come from the
  per-profile ``credentials.age`` blob; they are decrypted on demand,
  used once, and never echoed back. On success the runner saves
  ``storage_state.json`` AND records the login config in the profile
  meta so subsequent requests can auto-retry the login on a 401.
- ``session_status`` — cheap fetch against a configured probe URL
  that returns ``{status: authed | unauthed | unknown}``. Lets agents
  decide whether to call ``login`` without paying for a full
  navigation.

Tests inject fakes via the ``browser_factory`` / ``context_factory`` /
``page_factory`` keyword args; production callers leave them ``None``
and get the real Playwright bindings.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from helpers.credentials import Credential, CredentialError, get_credential
from helpers.profile_manager import Profile
from helpers.profile_meta import get_login_config, login_enabled, set_login_config
from helpers.secrets import SecretBundle

logger = logging.getLogger(__name__)

# Hard upper bound on a single verb's runtime. Each Playwright call
# (goto, screenshot, locator.text_content) takes its own timeout from
# this budget — we never want a single bad request to pin Chromium
# longer than this.
_DEFAULT_TIMEOUT_MS = int(os.environ.get("BROWSER_USE_VERB_TIMEOUT_MS", "30000"))

# Per-locator wait for ``extract``. A missing selector should *not*
# stall the whole verb; we cap each lookup at this and report
# ``None`` for selectors that don't resolve in time.
_EXTRACT_PER_SELECTOR_TIMEOUT_MS = int(os.environ.get("BROWSER_USE_EXTRACT_TIMEOUT_MS", "5000"))

# Default per-call timeout for ``login`` (issue #147 contract). Overridable
# per-request via payload["timeout_ms"]; bounded by the global cap above.
_DEFAULT_LOGIN_TIMEOUT_MS = int(os.environ.get("BROWSER_USE_LOGIN_TIMEOUT_MS", "15000"))


class VerbRunError(RuntimeError):
    """Raised when a verb cannot run (bad payload).

    Distinct from :class:`MalformedProfileError` so the server can map
    each to the right HTTP code — bad payload is 400 from the caller,
    malformed profile is 400 about the operator's on-disk state.
    """


class MalformedProfileError(ValueError):
    """Raised when the decrypted profile state is unusable.

    Surfaced before any browser is spawned so we don't pay the cost of
    launching Chromium just to crash inside it.
    """


class LoginNotEnabledError(VerbRunError):
    """Raised when ``login`` / ``session_status`` is called against a
    profile that has not opted in via ``meta.json``'s ``login_enabled``.

    The roll-out plan in issue #147 keeps the login surface off by
    default; the operator flips it on per-profile after credentials
    are in place. Refusing here means a stray ``login`` call against
    the wrong profile fails loudly rather than silently submitting
    forms with the wrong creds.
    """


class MfaRequiredError(RuntimeError):
    """Raised when the login flow detects an MFA / TOTP / WebAuthn prompt.

    v1 of the issue hard-fails (locked decision #2) — there's no path
    forward without a TOTP-aware contract, and silently retrying would
    risk locking out the upstream account.
    """


def validate_profile_state(profile: Profile) -> Path:
    """Fail fast if the decrypted profile state on disk is unusable.

    Returns the cookies-file path the context should load from / save
    to (which may not yet exist — the first run writes it). Raises
    :class:`MalformedProfileError` if a cookies file is present but
    isn't a JSON array; in that case we refuse to start a browser at
    all, because ``context.add_cookies`` would either silently drop
    the session or crash mid-run with a confusing error.
    """
    cookies_file = profile.path / "cookies.json"
    if not cookies_file.exists():
        return cookies_file
    try:
        raw = cookies_file.read_text()
    except OSError as e:
        raise MalformedProfileError(f"profile {profile.name!r} cookies.json unreadable: {e}") from e
    if not raw.strip():
        # Empty file is fine — treat as "no cookies yet" rather than
        # rejecting it.
        return cookies_file
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as e:
        raise MalformedProfileError(
            f"profile {profile.name!r} cookies.json is not valid JSON: {e.msg} at line {e.lineno}"
        ) from e
    if not isinstance(decoded, list):
        raise MalformedProfileError(
            f"profile {profile.name!r} cookies.json must be a JSON array of cookie "
            f"objects, got {type(decoded).__name__}"
        )
    return cookies_file


# ── Default Playwright factories ────────────────────────────────────


class _OwnedBrowser:
    """Couple a Playwright browser with its parent ``async_playwright`` handle.

    Playwright's async API splits "start the driver" from "launch a
    browser". The runner only ever sees one object, so we wrap both
    behind ``new_context`` / ``close`` and let the runner ignore the
    distinction. The ``close`` call tears both down — first the
    browser, then the driver — even if the browser-close raises.
    """

    def __init__(self, pw_handle, browser) -> None:
        self._pw = pw_handle
        self._browser = browser

    async def new_context(self, **kwargs):
        return await self._browser.new_context(**kwargs)

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._pw.stop()


async def _build_browser():
    """Start Playwright + launch headless Chromium.

    Imported lazily so tests that swap in a fake browser don't pull
    Playwright onto the test path.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    return _OwnedBrowser(pw, browser)


def _storage_state_path(profile_path: Path) -> Path:
    return profile_path / "storage_state.json"


async def _build_context(browser, cookies_file: Path):
    """Create a fresh context and pre-load it with the profile's session.

    Issue #147 added ``storage_state.json`` (cookies + localStorage +
    sessionStorage) — required for sites that put auth in localStorage
    rather than cookies. We prefer storage_state.json when present,
    fall back to the legacy cookies.json so older profiles keep
    working without a forced re-login.
    """
    profile_path = cookies_file.parent
    storage_state_file = _storage_state_path(profile_path)

    if storage_state_file.exists():
        raw = storage_state_file.read_text().strip()
        if raw:
            try:
                state = json.loads(raw)
                # Playwright accepts the dict directly via ``storage_state=``.
                context = await browser.new_context(storage_state=state)
                return context
            except (json.JSONDecodeError, TypeError):
                # Same fallback shape as cookies.json: validate_profile_state
                # already vetted cookies.json before we got here. If
                # storage_state.json is malformed we drop it and fall
                # through to cookies-only loading rather than crashing.
                logger.warning(
                    "storage_state.json at %s is malformed; falling back to cookies.json",
                    storage_state_file,
                )

    context = await browser.new_context()
    if cookies_file.exists():
        raw = cookies_file.read_text().strip()
        if raw:
            try:
                cookies = json.loads(raw)
            except json.JSONDecodeError:
                # ``validate_profile_state`` already ran; if we end up
                # here the file was rewritten mid-flight. Treat as no
                # cookies rather than crashing the context.
                cookies = []
            if cookies:
                await context.add_cookies(cookies)
    return context


async def _build_page(context):
    return await context.new_page()


async def _save_storage_state(context, cookies_file: Path) -> None:
    """Persist the context's full storage state back to the profile dir.

    Writes both ``storage_state.json`` (cookies + localStorage +
    sessionStorage) and the legacy ``cookies.json`` so a downgrade to
    a sidecar that doesn't know about storage_state.json still picks
    up the session. Failure is non-fatal — the verb already succeeded.
    """
    profile_path = cookies_file.parent
    storage_state_file = _storage_state_path(profile_path)

    # Try the full storage_state path first. Older Playwright contexts
    # / fakes may not implement ``storage_state``; degrade to cookies-only.
    storage_state = None
    if hasattr(context, "storage_state"):
        try:
            storage_state = await context.storage_state()
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("could not read storage_state from context: %s", e)

    try:
        if storage_state is not None:
            profile_path.mkdir(parents=True, exist_ok=True)
            storage_state_file.write_text(json.dumps(storage_state, indent=2))
            cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else None
        else:
            try:
                cookies = await context.cookies()
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("could not read cookies from context: %s", e)
                return
        if cookies is not None:
            cookies_file.parent.mkdir(parents=True, exist_ok=True)
            cookies_file.write_text(json.dumps(cookies, indent=2))
    except OSError as e:  # pragma: no cover — defensive
        logger.warning("could not persist session state for %s: %s", profile_path, e)


# ── Verb implementations ────────────────────────────────────────────


async def _navigate(page, payload: dict[str, Any]) -> dict[str, Any]:
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise VerbRunError("navigate requires 'url'")
    response = await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)
    return {"final_url": page.url, "_status_code": _response_status(response)}


async def _extract(page, payload: dict[str, Any]) -> dict[str, Any]:
    selectors = payload.get("selectors") or {}
    url = payload.get("url")
    if not isinstance(selectors, dict):
        raise VerbRunError("extract 'selectors' must be an object mapping name -> CSS selector")
    if not selectors:
        raise VerbRunError("extract requires at least one entry in 'selectors'")
    response = None
    if isinstance(url, str) and url:
        response = await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)

    extracted: dict[str, str | None] = {}
    for name, css in selectors.items():
        if not isinstance(css, str) or not css:
            extracted[str(name)] = None
            continue
        try:
            text = await page.locator(css).first.text_content(timeout=_EXTRACT_PER_SELECTOR_TIMEOUT_MS)
        except Exception:
            text = None
        extracted[str(name)] = text
    return {"extracted": extracted, "final_url": page.url, "_status_code": _response_status(response)}


async def _screenshot(page, payload: dict[str, Any]) -> dict[str, Any]:
    url = payload.get("url")
    full_page = bool(payload.get("full_page", True))
    response = None
    if isinstance(url, str) and url:
        response = await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)
    raw = await page.screenshot(full_page=full_page)
    if not isinstance(raw, (bytes, bytearray)):
        raise VerbRunError(f"page.screenshot returned {type(raw).__name__}, expected bytes")
    return {
        "screenshot_b64": base64.b64encode(bytes(raw)).decode("ascii"),
        "screenshot_bytes": len(raw),
        "final_url": page.url,
        "_status_code": _response_status(response),
    }


def _response_status(response: Any) -> int | None:
    """Best-effort HTTP status from a Playwright Response (or None)."""
    if response is None:
        return None
    status = getattr(response, "status", None)
    if callable(status):
        try:
            status = status()
        except Exception:
            return None
    return status if isinstance(status, int) else None


def _validate_login_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str], str, int]:
    """Pull and validate the four fields ``login`` reads from the payload.

    Returns ``(url, selectors, credential_key, timeout_ms)``. Selectors
    must include ``username``, ``password``, ``submit``, ``success``;
    optional ``mfa`` and ``login_url`` keys are passed through to the
    caller. Bad shapes raise :class:`VerbRunError` so the server maps
    them to a 400 — keeps malformed requests from spawning a browser.
    """
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise VerbRunError("login requires 'url' (the login page)")

    selectors = payload.get("selectors") or {}
    if not isinstance(selectors, dict):
        raise VerbRunError("login 'selectors' must be an object")
    required = ("username", "password", "submit", "success")
    for key in required:
        value = selectors.get(key)
        if not isinstance(value, str) or not value:
            raise VerbRunError(f"login 'selectors.{key}' must be a non-empty CSS string")

    credential_key = payload.get("credential_key")
    if not isinstance(credential_key, str) or not credential_key:
        raise VerbRunError("login requires 'credential_key' (the lookup name in credentials.age)")

    timeout_raw = payload.get("timeout_ms", _DEFAULT_LOGIN_TIMEOUT_MS)
    if not isinstance(timeout_raw, int) or timeout_raw <= 0:
        raise VerbRunError("login 'timeout_ms' must be a positive integer")
    # Cap at the global verb timeout so a runaway request can't pin
    # Chromium beyond the operator's intended ceiling.
    timeout_ms = min(timeout_raw, _DEFAULT_TIMEOUT_MS)

    return url, dict(selectors), credential_key, timeout_ms


async def _perform_login(
    page,
    *,
    url: str,
    selectors: dict[str, str],
    credential: Credential,
    timeout_ms: int,
) -> None:
    """Drive the actual login flow against ``page``.

    Steps:
    1. Navigate to ``url``.
    2. Type the username and password.
    3. Click submit.
    4. Wait for ``selectors.success`` to be visible within ``timeout_ms``.

    MFA detection: if ``selectors.mfa`` is set and that selector
    becomes visible before the success selector, raise
    :class:`MfaRequiredError` so the server can return ``error_type:
    mfa_required``. The username has already been entered at this
    point but the password may not have been — neither is captured
    in the error or in any logs (locked decision #5: no screenshot,
    no DOM, no payload).
    """
    await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)

    # ``locator.fill`` clears the field first — no need for an explicit
    # clear. We type the username before checking MFA so a site that
    # shows MFA only after the username is recognised still trips the
    # detector before the password is entered.
    await page.locator(selectors["username"]).first.fill(credential.username, timeout=timeout_ms)

    mfa_selector = selectors.get("mfa")
    if isinstance(mfa_selector, str) and mfa_selector:
        try:
            mfa_locator = page.locator(mfa_selector).first
            # Cheap visibility check with a short timeout — we don't
            # want to wait the full login budget on every MFA probe.
            if await mfa_locator.is_visible(timeout=500):
                raise MfaRequiredError(
                    f"MFA challenge detected via selector {mfa_selector!r}; "
                    "v1 of the login verb does not support TOTP / WebAuthn"
                )
        except MfaRequiredError:
            raise
        except Exception:
            # Selector not present yet — the success path is the
            # expected outcome; carry on.
            pass

    await page.locator(selectors["password"]).first.fill(credential.password, timeout=timeout_ms)
    await page.locator(selectors["submit"]).first.click(timeout=timeout_ms)

    # MFA can also surface after submit (the more common case). Race
    # the success and MFA selectors; MFA wins if it resolves at all
    # (locked decision #2 — hard-fail). We do not capture the page
    # DOM here — locked decision #5.
    if isinstance(mfa_selector, str) and mfa_selector:
        import asyncio

        success_locator = page.locator(selectors["success"]).first
        mfa_locator = page.locator(mfa_selector).first

        async def _wait(loc):
            await loc.wait_for(timeout=timeout_ms)

        success_task = asyncio.create_task(_wait(success_locator))
        mfa_task = asyncio.create_task(_wait(mfa_locator))
        try:
            done, pending = await asyncio.wait(
                {success_task, mfa_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (success_task, mfa_task):
                if not task.done():
                    task.cancel()
        # Drain cancellations so they don't surface as warnings.
        for task in (success_task, mfa_task):
            if task.cancelled():
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # MFA wins outright: a visible MFA prompt means the password
        # was accepted but the account is gated. Hard-fail.
        if mfa_task in done and not mfa_task.cancelled() and mfa_task.exception() is None:
            raise MfaRequiredError(
                f"MFA challenge detected via selector {mfa_selector!r} after submit; "
                "v1 of the login verb does not support TOTP / WebAuthn"
            )
        # Success path.
        if success_task in done and not success_task.cancelled() and success_task.exception() is None:
            return
        # Both failed (or success failed and MFA didn't trip): surface
        # the success-side error so the caller gets a real reason.
        if success_task in done and success_task.exception() is not None:
            raise success_task.exception()  # type: ignore[misc]
        # Edge: MFA raised but success didn't complete — shouldn't
        # happen under FIRST_COMPLETED, but fall through to the simple
        # wait below as a safety net.

    await page.locator(selectors["success"]).first.wait_for(timeout=timeout_ms)


async def _login(
    page,
    payload: dict[str, Any],
    *,
    profile: Profile,
    context: Any,
) -> dict[str, Any]:
    """Implementation of the ``login`` verb.

    Pulls the credential from the profile's ``credentials.age`` blob,
    drives the form, then writes the resulting authenticated session
    back to ``storage_state.json`` and caches the login config in the
    profile meta so later requests can auto-retry on 401.
    """
    if not login_enabled(profile.path):
        raise LoginNotEnabledError(
            f"profile {profile.name!r} has not opted into the login verb. "
            "Set 'login_enabled: true' in its meta.json after running "
            "`grant <profile> credentials <key>` from Slack."
        )

    url, selectors, credential_key, timeout_ms = _validate_login_payload(payload)

    try:
        credential = get_credential(profile.path, credential_key)
    except CredentialError as e:
        raise VerbRunError(str(e)) from e

    await _perform_login(
        page,
        url=url,
        selectors=selectors,
        credential=credential,
        timeout_ms=timeout_ms,
    )

    # Success — persist the session AND remember the login config so
    # subsequent verbs can auto-retry on a 401.
    cookies_file = profile.path / "cookies.json"
    await _save_storage_state(context, cookies_file)
    set_login_config(
        profile.path,
        {
            "url": url,
            "selectors": selectors,
            "credential_key": credential_key,
            "timeout_ms": timeout_ms,
        },
    )
    return {"session_persisted": True}


async def _session_status(page, payload: dict[str, Any]) -> dict[str, Any]:
    """Cheap probe — fetch ``probe_url`` and classify the response.

    Returns ``{"status": "authed" | "unauthed" | "unknown"}``.

    - 2xx → ``authed``
    - 401 / 403, or a redirect that lands on ``selectors.login_url`` →
      ``unauthed``
    - anything else (network blip, 5xx) → ``unknown``

    Doesn't open a fresh tab or take a screenshot — the agent uses
    this to decide whether to call ``login`` cheaply.
    """
    probe_url = payload.get("probe_url")
    if not isinstance(probe_url, str) or not probe_url:
        raise VerbRunError("session_status requires 'probe_url'")
    login_url = payload.get("login_url")

    try:
        response = await page.goto(probe_url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)
    except Exception:
        return {"status": "unknown", "final_url": page.url}

    final_url = page.url
    status_code = _response_status(response)
    if isinstance(login_url, str) and login_url and final_url.startswith(login_url):
        return {"status": "unauthed", "final_url": final_url, "_status_code": status_code}
    if status_code is None:
        return {"status": "unknown", "final_url": final_url}
    if status_code in (401, 403):
        return {"status": "unauthed", "final_url": final_url, "_status_code": status_code}
    if 200 <= status_code < 300:
        return {"status": "authed", "final_url": final_url, "_status_code": status_code}
    return {"status": "unknown", "final_url": final_url, "_status_code": status_code}


_SimpleVerbFn = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]

# Verbs that take just (page, payload) — i.e. don't need the profile
# or the context for storage persistence. Login / session_status need
# more and are dispatched separately below.
_SIMPLE_VERBS: dict[str, _SimpleVerbFn] = {
    "navigate": _navigate,
    "extract": _extract,
    "screenshot": _screenshot,
}

_LOGIN_VERB = "login"
_SESSION_STATUS_VERB = "session_status"


def known_verbs() -> frozenset[str]:
    """Return the set of supported verb names. The server uses this to map unknown verbs to 400."""
    return frozenset(_SIMPLE_VERBS) | {_LOGIN_VERB, _SESSION_STATUS_VERB}


# ── Auto-retry on 401 (issue #147 decision #4) ──────────────────────


def _is_unauthed(result: dict[str, Any], login_url: str | None) -> bool:
    """Decide if a navigate/extract/screenshot result counts as a logged-out signal.

    Returns True when the response landed on a login redirect URL or
    came back with a 401/403. The runner uses this to decide whether
    to auto-retry the verb after a fresh ``login``.
    """
    if not isinstance(result, dict):
        return False
    status = result.get("_status_code")
    if status in (401, 403):
        return True
    if login_url and isinstance(result.get("final_url"), str) and result["final_url"].startswith(login_url):
        return True
    return False


async def _retry_login(
    page,
    profile: Profile,
    context: Any,
) -> bool:
    """Re-run the cached login config. Returns True on success.

    Used by navigate/extract/screenshot when they detect a logged-out
    state. We never raise from here — the original verb's failure is
    what the agent should see; a retry that itself fails just means we
    leave the original error in place.
    """
    cfg = get_login_config(profile.path)
    if not cfg:
        return False
    try:
        url = cfg["url"]
        selectors = cfg["selectors"]
        credential_key = cfg["credential_key"]
        timeout_ms = int(cfg.get("timeout_ms", _DEFAULT_LOGIN_TIMEOUT_MS))
    except (KeyError, TypeError, ValueError):
        return False
    try:
        credential = get_credential(profile.path, credential_key)
    except CredentialError:
        return False
    try:
        await _perform_login(
            page,
            url=url,
            selectors=selectors,
            credential=credential,
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        logger.info("auto-retry login failed for profile %s: %s", profile.name, type(e).__name__)
        return False
    cookies_file = profile.path / "cookies.json"
    await _save_storage_state(context, cookies_file)
    return True


# ── Public entry point ──────────────────────────────────────────────


async def run_verb(
    *,
    verb: str,
    profile: Profile,
    bundle: SecretBundle,
    payload: dict[str, Any],
    browser_factory: Callable[[], Awaitable[Any]] | None = None,
    context_factory: Callable[[Any, Path], Awaitable[Any]] | None = None,
    page_factory: Callable[[Any], Awaitable[Any]] | None = None,
    save_cookies: Callable[[Any, Path], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Execute one verb against a fresh per-profile Playwright session.

    Returns a structured ``dict``:

    - On success: ``{"status": "ok", "action": <verb>, "profile": ...,
      <verb-specific keys>}``.
    - On any setup or runtime failure (missing Chromium, profile dir
      EACCES, Playwright launch crash, navigation timeout, DNS,
      locator crash): ``{"status": "error", "action": ..., "profile":
      ..., "error": <scrubbed message>, "error_type": ...}``. The
      handler still gets a 200 so the agent can read the structured
      body instead of a 5xx.

    Raises :class:`MalformedProfileError` / :class:`VerbRunError` for
    upfront refusals (the server maps both to 400). The ``*_factory``
    keyword args are the test-injection seam; production leaves them
    ``None``.
    """
    if verb not in known_verbs():
        raise VerbRunError(f"unknown verb: {verb!r}")

    # session_status & login both need the opt-in flag.
    if verb in {_LOGIN_VERB, _SESSION_STATUS_VERB} and not login_enabled(profile.path):
        raise LoginNotEnabledError(
            f"profile {profile.name!r} has not opted into the login verb. "
            "Set 'login_enabled: true' in its meta.json after running "
            "`grant <profile> credentials <key>` from Slack."
        )

    cookies_file = validate_profile_state(profile)

    browser = None
    context = None
    page = None
    try:
        try:
            browser = await (browser_factory or _build_browser)()
            context = await (context_factory or _build_context)(browser, cookies_file)
            page = await (page_factory or _build_page)(context)

            verb_result = await _dispatch_verb(
                verb=verb,
                page=page,
                context=context,
                profile=profile,
                payload=payload,
            )
        except (VerbRunError, LoginNotEnabledError):
            # Caller-side payload bug — surface as 400 via the server.
            raise
        except MfaRequiredError as e:
            # Locked decision #2: hard-fail with a distinct error_type.
            # Never include credential material in the message — the
            # error itself doesn't carry it, but scrub anyway as a
            # belt-and-braces guard.
            message = bundle.scrub(str(e)) if bundle.values else str(e)
            logger.info("verb=%s profile=%s mfa_required", verb, profile.name)
            return {
                "status": "error",
                "action": verb,
                "profile": profile.name,
                "error": message,
                "error_type": "mfa_required",
            }
        except Exception as e:
            # Setup failure (Chromium missing, profile dir EACCES,
            # Playwright bump) or runtime failure (navigation timeout,
            # DNS, locator crash). Scrub the message and return a
            # structured error so the handler doesn't see a 500.
            message = bundle.scrub(str(e)) if bundle.values else str(e)
            logger.info("verb=%s profile=%s failed: %s", verb, profile.name, type(e).__name__)
            return {
                "status": "error",
                "action": verb,
                "profile": profile.name,
                "error": message,
                "error_type": type(e).__name__,
            }

        # Verb succeeded — persist the full session state so cookies
        # AND localStorage stick across requests. ``login`` already
        # wrote storage_state inside ``_dispatch_verb`` so re-saving
        # here is a no-op for that case but keeps the contract
        # consistent across all verbs.
        await (save_cookies or _save_storage_state)(context, cookies_file)
        # Strip the internal ``_status_code`` field before returning to
        # the caller — it's an implementation detail of the auto-retry
        # path, not part of the verb's public contract.
        if isinstance(verb_result, dict) and "_status_code" in verb_result:
            verb_result = {k: v for k, v in verb_result.items() if k != "_status_code"}
        return {"status": "ok", "action": verb, "profile": profile.name, **verb_result}
    finally:
        # Close in reverse order. Each close is independent so a hang
        # in one doesn't take down the others. Exceptions are swallowed
        # — we'd rather report the original verb result than mask it
        # with a teardown error.
        if page is not None:
            try:
                close = getattr(page, "close", None)
                if callable(close):
                    res = close()
                    if hasattr(res, "__await__"):
                        await res
            except Exception as close_err:  # pragma: no cover — defensive
                logger.warning("failed to close page: %s", close_err)
        if context is not None:
            try:
                close = getattr(context, "close", None)
                if callable(close):
                    res = close()
                    if hasattr(res, "__await__"):
                        await res
            except Exception as close_err:  # pragma: no cover — defensive
                logger.warning("failed to close context: %s", close_err)
        if browser is not None:
            try:
                close = getattr(browser, "close", None)
                if callable(close):
                    res = close()
                    if hasattr(res, "__await__"):
                        await res
            except Exception as close_err:  # pragma: no cover — defensive
                logger.warning("failed to close browser: %s", close_err)


async def _dispatch_verb(
    *,
    verb: str,
    page: Any,
    context: Any,
    profile: Profile,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Route to the right verb fn and apply auto-retry-on-401 where it makes sense.

    ``login`` and ``session_status`` are special-cased — they need the
    profile / context, and they don't get the auto-retry treatment
    (login is the retry target; session_status is itself the probe).
    """
    if verb == _LOGIN_VERB:
        return await _login(page, payload, profile=profile, context=context)
    if verb == _SESSION_STATUS_VERB:
        return await _session_status(page, payload)

    fn = _SIMPLE_VERBS[verb]
    result = await fn(page, payload)
    # Auto-retry on 401 / login-redirect (locked decision #4). Only if
    # this profile has a cached login config from a prior login() call.
    cfg = get_login_config(profile.path)
    login_url = cfg.get("url") if isinstance(cfg, dict) else None
    if cfg and _is_unauthed(result, login_url):
        retried = await _retry_login(page, profile, context)
        if retried:
            second = await fn(page, payload)
            if not _is_unauthed(second, login_url):
                return second
            # Two consecutive logged-out signals after a fresh login →
            # surface the second result as-is. The agent sees the
            # 401-flavoured response without us pretending the retry
            # worked.
            return second
    return result
