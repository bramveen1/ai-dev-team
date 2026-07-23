"""Shared GitHub REST helpers for the router's autonomous loops.

One copy of the thin ``httpx`` wrappers that were previously duplicated
byte-for-byte in ``router/auto_dispatch.py`` and ``router/merge_queue.py``.
Both modules re-export these under their old private names so existing
call sites and test patch targets keep working.

The PAT-file reader itself (``read_token_file``/``TokenError``) lives in
``packs/dispatch/token_reader.py`` — not here — because that's the one
module mounted into both the worker containers and the router container
(see ``token_reader``'s docstring, and #718). It's loaded below via the
same sys.path-injection pattern already used for ``quota.py`` in
``router/dispatch/supervision.py``.
"""

from __future__ import annotations

import logging
import sys as _sys
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PACK_DIR = Path(__file__).resolve().parent.parent / "packs" / "dispatch"
if _PACK_DIR.is_dir() and str(_PACK_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PACK_DIR))

from token_reader import TokenError, read_token_file  # noqa: E402

GITHUB_API_BASE = "https://api.github.com"

# Default PAT file location — one-line change to swap the merge identity.
MERGE_PAT_PATH = "/config/secrets/gh-aidt-merge.token"

# Timeout (seconds) applied to every GitHub API call.
DEFAULT_TIMEOUT_SECONDS = 30

# Hard cap on pages followed by gh_get_all — a backstop against a runaway
# Link: rel="next" chain, not a expected ceiling (list endpoints used by this
# codebase are nowhere near 100 * per_page items).
MAX_PAGES = 100

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "GITHUB_API_BASE",
    "MAX_PAGES",
    "MERGE_PAT_PATH",
    "TokenError",
    "auth_headers",
    "gh_get",
    "gh_get_all",
    "gh_post",
    "gh_put",
    "read_pat",
    "read_token_file",
]


def read_pat(path: str = MERGE_PAT_PATH) -> str:
    """Read the GitHub PAT from *path*.

    Raises :class:`TokenError` if the file is missing, unreadable, empty,
    or contains only comment/blank lines. Never silently falls through to
    cached credentials — that is the #298 footgun this check was designed
    to prevent.
    """
    token = read_token_file(path, skip_comments=True, raise_on_empty=True)
    assert token is not None  # raise_on_empty guarantees this
    return token


def auth_headers(pat: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def gh_get(path: str, pat: str, **params: Any) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        return await client.get(url, headers=auth_headers(pat), params=params)


async def gh_get_all(path: str, pat: str, *, max_pages: int = MAX_PAGES, **params: Any) -> list[dict]:
    """Fetch every page of a paginated GitHub list endpoint.

    Follows the response's ``Link: rel="next"`` header, concatenating each
    page's JSON array in the order returned (page 1 first) until no ``next``
    link remains. Uses the same timeout and auth headers as :func:`gh_get`,
    raises :class:`TokenError` on a 401 exactly like ``gh_get`` callers do,
    and calls ``raise_for_status`` for any other non-2xx response.

    ``max_pages`` is a hard, logged backstop against runaway pagination — it
    does not silently truncate; hitting it is logged as a warning.
    """
    params.setdefault("per_page", 100)
    results: list[dict] = []
    url = f"{GITHUB_API_BASE}{path}"
    next_params: dict[str, Any] | None = params
    pages = 0
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        while url:
            resp = await client.get(url, headers=auth_headers(pat), params=next_params)
            if resp.status_code == 401:
                raise TokenError(f"GitHub 401 fetching {path}")
            resp.raise_for_status()
            results.extend(resp.json())
            pages += 1
            url = resp.links.get("next", {}).get("url")
            next_params = None  # the next URL already carries its own query string
            if url and pages >= max_pages:
                logger.warning(
                    "gh_get_all: hit max_pages=%d for %s — stopping pagination, results may be incomplete",
                    max_pages,
                    path,
                )
                break
    return results


async def gh_put(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        return await client.put(url, headers=auth_headers(pat), json=body or {})


async def gh_post(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        return await client.post(url, headers=auth_headers(pat), json=body or {})
