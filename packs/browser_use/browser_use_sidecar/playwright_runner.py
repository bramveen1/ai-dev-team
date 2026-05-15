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

3. **Wire the decrypted profile into the context.** Cookies from
   ``<profile>/cookies.json`` are loaded into the new context before
   any verb runs; on success, the updated cookies are written back so
   the session sticks across requests. Profile state is validated
   *before* any browser is spawned — a malformed ``cookies.json``
   trips a fast-fail.

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

from helpers.profile_manager import Profile
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


async def _build_context(browser, cookies_file: Path):
    """Create a fresh context and pre-load it with the profile's cookies."""
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


async def _save_cookies(context, cookies_file: Path) -> None:
    """Persist the context's cookies back to the profile dir.

    Runs on the success path of each verb so an authenticated session
    survives across requests. Failure here is non-fatal — the verb
    already succeeded; logging it is enough.
    """
    try:
        cookies = await context.cookies()
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("could not read cookies from context: %s", e)
        return
    try:
        cookies_file.parent.mkdir(parents=True, exist_ok=True)
        cookies_file.write_text(json.dumps(cookies, indent=2))
    except OSError as e:  # pragma: no cover — defensive
        logger.warning("could not persist cookies to %s: %s", cookies_file, e)


# ── Verb implementations ────────────────────────────────────────────


async def _navigate(page, payload: dict[str, Any]) -> dict[str, Any]:
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise VerbRunError("navigate requires 'url'")
    await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)
    return {"final_url": page.url}


async def _extract(page, payload: dict[str, Any]) -> dict[str, Any]:
    selectors = payload.get("selectors") or {}
    url = payload.get("url")
    if not isinstance(selectors, dict):
        raise VerbRunError("extract 'selectors' must be an object mapping name -> CSS selector")
    if not selectors:
        raise VerbRunError("extract requires at least one entry in 'selectors'")
    if isinstance(url, str) and url:
        await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)

    extracted: dict[str, str | None] = {}
    for name, css in selectors.items():
        if not isinstance(css, str) or not css:
            # A non-string selector is a caller bug, but degrading
            # gracefully (None) is friendlier than failing the whole
            # extract — the per-key result tells the caller which one
            # was wrong.
            extracted[str(name)] = None
            continue
        try:
            text = await page.locator(css).first.text_content(timeout=_EXTRACT_PER_SELECTOR_TIMEOUT_MS)
        except Exception:
            # Missing selectors return None — that's the contract from
            # the issue. Don't fail the whole verb on one miss.
            text = None
        extracted[str(name)] = text
    return {"extracted": extracted, "final_url": page.url}


async def _screenshot(page, payload: dict[str, Any]) -> dict[str, Any]:
    url = payload.get("url")
    full_page = bool(payload.get("full_page", True))
    if isinstance(url, str) and url:
        await page.goto(url, wait_until="load", timeout=_DEFAULT_TIMEOUT_MS)
    raw = await page.screenshot(full_page=full_page)
    if not isinstance(raw, (bytes, bytearray)):
        raise VerbRunError(f"page.screenshot returned {type(raw).__name__}, expected bytes")
    return {
        "screenshot_b64": base64.b64encode(bytes(raw)).decode("ascii"),
        "screenshot_bytes": len(raw),
        "final_url": page.url,
    }


_VERBS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "navigate": _navigate,
    "extract": _extract,
    "screenshot": _screenshot,
}


def known_verbs() -> frozenset[str]:
    """Return the set of supported verb names. The server uses this to map unknown verbs to 400."""
    return frozenset(_VERBS)


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
    if verb not in _VERBS:
        raise VerbRunError(f"unknown verb: {verb!r}")

    cookies_file = validate_profile_state(profile)

    browser = None
    context = None
    page = None
    try:
        try:
            browser = await (browser_factory or _build_browser)()
            context = await (context_factory or _build_context)(browser, cookies_file)
            page = await (page_factory or _build_page)(context)

            verb_fn = _VERBS[verb]
            verb_result = await verb_fn(page, payload)
        except VerbRunError:
            # Caller-side payload bug — surface as 400 via the server.
            raise
        except Exception as e:
            # Setup failure (Chromium missing, profile dir EACCES,
            # Playwright bump) or runtime failure (navigation timeout,
            # DNS, locator crash). Scrub the message and return a
            # structured error so the handler doesn't see a 500.
            message = bundle.scrub(str(e)) if bundle.values else str(e)
            logger.info("verb=%s profile=%s failed: %s", verb, profile.name, message)
            return {
                "status": "error",
                "action": verb,
                "profile": profile.name,
                "error": message,
                "error_type": type(e).__name__,
            }

        # Verb succeeded — persist cookies so the session sticks.
        await (save_cookies or _save_cookies)(context, cookies_file)
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
