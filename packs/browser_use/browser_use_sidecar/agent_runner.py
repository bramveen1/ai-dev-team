"""Run one ``browser_use.Agent`` invocation behind a stable interface.

This module is the single seam between the FastAPI app (``server.py``)
and the Browser Use library. Splitting it out keeps two things tractable:

1. **Tests can mock at one boundary.** The sidecar's unit tests patch
   :func:`run_agent` (and the LLM / Browser factories below) rather
   than importing Browser Use into the test runner. Browser Use pulls
   in Playwright + Chromium dependencies that we don't want in the CI
   image.
2. **Lifecycle is owned here.** ``Browser`` and ``BrowserContext`` are
   opened and closed in this file — every code path through
   :func:`run_agent` runs the close calls in ``finally`` so a crash
   during ``Agent.run`` doesn't leak a Chromium process. That's the
   "owned-client close path on success and exception" criterion from
   issue #119.

The runner translates the pack's three read verbs into Agent task
prompts:

- ``navigate`` → "Open the given URL and report the final URL after
  redirects." The Agent is allowed to dismiss cookie banners on the
  way, which is why we use the Agent loop instead of raw Playwright.
- ``extract`` → "Open the URL (if given) and extract the requested
  selectors. Return a mapping of selector name → text."
- ``screenshot`` → "Open the URL (if given) and capture a full-page
  screenshot."

Each verb produces a structured response with the action name, the
profile, a ``status`` (``ok`` / ``error``), and verb-specific fields
(``final_url``, ``extracted``, ``screenshot_path``). Errors from the
Agent (timeouts, navigation failures) become ``status="error"`` plus
a scrubbed message — never a raw traceback, never a 500.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from helpers.profile_manager import Profile
from helpers.secrets import SecretBundle

logger = logging.getLogger(__name__)

# Cap an Agent run so a navigation loop can't pin the sidecar forever.
# 12 steps is generous for the read verbs (load page → maybe dismiss
# banner → extract) while still bounding the worst case.
_DEFAULT_MAX_STEPS = int(os.environ.get("BROWSER_USE_MAX_STEPS", "12"))

# Default LLM model. The sidecar uses a fast model — read actions are
# cheap and we'd rather pay for low latency than for an over-thinking
# planner.
_DEFAULT_LLM_MODEL = os.environ.get("BROWSER_USE_LLM_MODEL", "claude-haiku-4-5-20251001")


class AgentRunError(RuntimeError):
    """Raised when the Agent cannot complete the action.

    Distinct from :class:`MalformedProfileError` so callers can
    distinguish "the agent ran and failed" (return a structured error
    body) from "we refused to start the browser" (return 4xx).
    """


class MalformedProfileError(ValueError):
    """Raised when the decrypted profile state is unusable.

    Surfaced before any browser is spawned so we don't pay the cost of
    launching Chromium just to crash inside it.
    """


def _build_llm():
    """Return the LLM the Agent uses to plan its actions.

    Imported lazily so tests that mock :func:`run_agent` never need
    ``langchain_anthropic`` on their path.
    """
    from langchain_anthropic import ChatAnthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("BROWSER_USE_LLM_API_KEY")
    if not api_key:
        raise AgentRunError(
            "no LLM API key configured for the browser-use sidecar; set "
            "ANTHROPIC_API_KEY (or BROWSER_USE_LLM_API_KEY) on the sidecar container"
        )
    return ChatAnthropic(model=_DEFAULT_LLM_MODEL, api_key=api_key, timeout=60, max_retries=2)


def _build_browser():
    """Return a headless Browser Use ``Browser`` instance.

    Imported lazily; see :func:`_build_llm`.
    """
    from browser_use import Browser, BrowserConfig

    return Browser(config=BrowserConfig(headless=True, disable_security=False))


def _build_browser_context(browser, cookies_file: Path):
    """Return a fresh ``BrowserContext`` for ``browser`` with cookies wired in."""
    from browser_use import BrowserContextConfig

    config = BrowserContextConfig(cookies_file=str(cookies_file))
    return browser.new_context(config=config)


def _build_agent(*, task: str, llm, browser, browser_context, sensitive_data: dict[str, str] | None):
    """Construct an Agent. Factored out for test seam parity with the others."""
    from browser_use import Agent

    return Agent(
        task=task,
        llm=llm,
        browser=browser,
        browser_context=browser_context,
        sensitive_data=sensitive_data or None,
    )


def validate_profile_state(profile: Profile) -> Path:
    """Fail fast if the decrypted profile state on disk is unusable.

    Returns the cookies-file path the Agent should use (which may not
    yet exist — the first run writes it). Raises
    :class:`MalformedProfileError` if the profile dir holds a cookies
    file that isn't valid JSON; in that case we refuse to start a
    browser at all, because the Agent would either ignore the cookies
    (silently logging the operator out) or crash mid-run with a
    confusing error.
    """
    cookies_file = profile.path / "cookies.json"
    if not cookies_file.exists():
        return cookies_file
    try:
        raw = cookies_file.read_text()
    except OSError as e:
        raise MalformedProfileError(f"profile {profile.name!r} cookies.json unreadable: {e}") from e
    if not raw.strip():
        # Empty file is fine — Browser Use treats "no cookies yet" as
        # an empty array. Don't reject it.
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


def build_task(action: str, payload: dict[str, Any]) -> str:
    """Map a pack action + caller payload to an Agent task description.

    Kept pure (no I/O, no globals) so the test suite can assert the
    mapping without spinning up an Agent.
    """
    if action == "navigate":
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("navigate requires 'url'")
        return (
            f"Open the URL {url} in the browser. Wait for the page to finish loading "
            "(including any redirects, login walls, or cookie banners that block content). "
            "When the page is settled, mark the task done and report the final URL."
        )
    if action == "extract":
        selectors = payload.get("selectors") or {}
        url = payload.get("url")
        if not isinstance(selectors, dict):
            raise ValueError("extract 'selectors' must be an object mapping name -> selector/description")
        if not selectors:
            raise ValueError("extract requires at least one entry in 'selectors'")
        target = f"Open the URL {url} in the browser. " if isinstance(url, str) and url else ""
        bullets = "\n".join(f"- {name}: {desc}" for name, desc in selectors.items())
        return (
            f"{target}Extract the following items from the current page and return them as a "
            "JSON object on completion:\n"
            f"{bullets}\n"
            "If a selector cannot be located, set its value to null rather than guessing."
        )
    if action == "screenshot":
        url = payload.get("url")
        target = f"Open the URL {url} in the browser. " if isinstance(url, str) and url else ""
        return (
            f"{target}Capture a full-page screenshot of the current page. "
            "Mark the task done once the screenshot has been taken."
        )
    raise ValueError(f"unknown action: {action!r}")


def _summarise_history(action: str, profile: Profile, history) -> dict[str, Any]:
    """Convert an ``AgentHistoryList`` into the JSON we ship to the handler.

    Pulls out: final URL, extracted content (if any), screenshot paths
    (if any), number of steps, and an explicit ``status`` field so the
    handler can distinguish "ran and produced data" from "ran and
    errored out internally".
    """
    result: dict[str, Any] = {
        "status": "ok",
        "action": action,
        "profile": profile.name,
    }

    # ``AgentHistoryList`` exposes typed accessors for the bits we care
    # about. Each one tolerates a missing field — Browser Use returns
    # empty lists rather than raising, which is what we want.
    try:
        urls = history.urls() if callable(getattr(history, "urls", None)) else []
    except Exception:  # pragma: no cover — defensive
        urls = []
    if urls:
        # The final visited URL is the most useful signal for the
        # caller. Earlier URLs in the trace are intermediate redirects.
        result["final_url"] = urls[-1]

    try:
        final = history.final_result() if callable(getattr(history, "final_result", None)) else None
    except Exception:  # pragma: no cover — defensive
        final = None
    if final:
        # ``final_result`` is the Agent's structured payload at task
        # completion. For ``extract`` this is the dict of selector
        # values; for ``navigate`` it's typically a short status line.
        result["result"] = final

    if action == "extract":
        try:
            extracted = history.extracted_content() if callable(getattr(history, "extracted_content", None)) else []
        except Exception:  # pragma: no cover — defensive
            extracted = []
        if extracted:
            result["extracted"] = extracted

    if action == "screenshot":
        try:
            screenshots = history.screenshots() if callable(getattr(history, "screenshots", None)) else []
        except Exception:  # pragma: no cover — defensive
            screenshots = []
        if screenshots:
            # Hand back paths only; the binary blobs stay on disk so
            # the sidecar response is small.
            result["screenshots"] = screenshots

    try:
        result["steps"] = int(history.number_of_steps()) if callable(getattr(history, "number_of_steps", None)) else 0
    except Exception:  # pragma: no cover — defensive
        result["steps"] = 0

    try:
        if callable(getattr(history, "has_errors", None)) and history.has_errors():
            result["status"] = "error"
            errors = history.errors() if callable(getattr(history, "errors", None)) else []
            # ``errors`` is a list of per-step error strings; join the
            # non-empty ones so the caller sees a concise message.
            joined = "; ".join(str(e) for e in errors if e)
            if joined:
                result["error"] = joined
    except Exception:  # pragma: no cover — defensive
        pass

    return result


async def run_agent(
    *,
    action: str,
    profile: Profile,
    bundle: SecretBundle,
    payload: dict[str, Any],
    max_steps: int | None = None,
    agent_factory: Callable[..., Any] | None = None,
    browser_factory: Callable[[], Any] | None = None,
    context_factory: Callable[[Any, Path], Awaitable[Any]] | None = None,
    llm_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Build, run, and tear down an Agent for one action.

    The ``*_factory`` keyword args exist so tests can inject doubles
    without monkey-patching at module scope. Production callers leave
    them ``None`` and get the real Browser Use bindings.
    """
    task = build_task(action, payload)
    cookies_file = validate_profile_state(profile)

    sensitive_data = dict(bundle.values) if bundle.values else None

    llm = (llm_factory or _build_llm)()
    browser = (browser_factory or _build_browser)()
    browser_context = None

    try:
        ctx_call = context_factory or _build_browser_context
        new_ctx = ctx_call(browser, cookies_file)
        # ``new_context`` is async on the real Browser Use API; tests
        # can hand us a plain object or an awaitable. Cover both.
        if hasattr(new_ctx, "__await__"):
            browser_context = await new_ctx
        else:
            browser_context = new_ctx

        agent = (agent_factory or _build_agent)(
            task=task,
            llm=llm,
            browser=browser,
            browser_context=browser_context,
            sensitive_data=sensitive_data,
        )

        try:
            history = await agent.run(max_steps=max_steps or _DEFAULT_MAX_STEPS)
        except Exception as e:
            # The Agent itself blew up (navigation timeout, LLM error,
            # selector miss). Convert to a structured error so the
            # handler doesn't see a 500 — and so any plaintext that
            # snuck into the exception message is scrubbed first.
            message = bundle.scrub(str(e)) if bundle.values else str(e)
            logger.info("agent run failed for action=%s profile=%s: %s", action, profile.name, message)
            return {
                "status": "error",
                "action": action,
                "profile": profile.name,
                "error": message,
                "error_type": type(e).__name__,
            }

        return _summarise_history(action, profile, history)
    finally:
        # Owned-client lifecycle: every Browser/Context we opened
        # closes here. Swallow individual close exceptions — we'd
        # rather report the original Agent error than mask it with a
        # teardown failure. The try/except pairs are intentionally
        # narrow so a hung close doesn't take down the other.
        if browser_context is not None:
            try:
                close = getattr(browser_context, "close", None)
                if callable(close):
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
            except Exception as close_err:  # pragma: no cover — defensive
                logger.warning("failed to close browser context: %s", close_err)
        try:
            close = getattr(browser, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
        except Exception as close_err:  # pragma: no cover — defensive
            logger.warning("failed to close browser: %s", close_err)
