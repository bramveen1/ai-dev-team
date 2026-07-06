"""Shared GitHub REST helpers for the router's autonomous loops.

One copy of the PAT-file reader and the thin ``httpx`` wrappers that were
previously duplicated byte-for-byte in ``router/auto_dispatch.py`` and
``router/merge_queue.py``. Both modules re-export these under their old
private names so existing call sites and test patch targets keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

GITHUB_API_BASE = "https://api.github.com"

# Default PAT file location — one-line change to swap the merge identity.
MERGE_PAT_PATH = "/config/secrets/gh-aidt-merge.token"

# Timeout (seconds) applied to every GitHub API call.
DEFAULT_TIMEOUT_SECONDS = 30


class TokenError(Exception):
    """The PAT file is missing, unreadable, or empty."""


def read_pat(path: str = MERGE_PAT_PATH) -> str:
    """Read the GitHub PAT from *path*.

    Raises :class:`TokenError` if the file is missing, unreadable, or empty.
    Never silently falls through to cached credentials — that is the #298
    footgun this check was designed to prevent.
    """
    try:
        token = Path(path).read_text().strip()
    except FileNotFoundError:
        raise TokenError(f"PAT file not found: {path}") from None
    except OSError as exc:
        raise TokenError(f"Cannot read PAT file {path}: {exc}") from exc
    if not token:
        raise TokenError(f"PAT file is empty: {path}")
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


async def gh_put(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        return await client.put(url, headers=auth_headers(pat), json=body or {})


async def gh_post(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        return await client.post(url, headers=auth_headers(pat), json=body or {})
