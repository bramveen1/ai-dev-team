"""Authentication and CSRF management for the Path to Hired API.

Login flow:
  1. POST /api/auth/login with username + password → sets session cookie.
  2. Extract X-CSRF-Token from the response header (or fetch it separately).

Refresh flow (on 401):
  1. POST /api/auth/refresh.
  2. Re-fetch X-CSRF-Token (the token does NOT survive the refresh).

CSRF rotation (on 403 with a CSRF error in the body):
  1. GET /api/csrf to obtain a fresh token.
  2. Retry the original request once.

429 from login is surfaced as :class:`RateLimitError` — no retry loop.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

CSRF_HEADER = "X-CSRF-Token"


class AuthError(RuntimeError):
    """Raised when login or session refresh fails."""


class RateLimitError(RuntimeError):
    """Raised when the login endpoint returns 429."""


def _extract_csrf(response: httpx.Response) -> str | None:
    return response.headers.get(CSRF_HEADER)


async def fetch_csrf(client: httpx.AsyncClient, base_url: str) -> str:
    """Fetch a fresh CSRF token from GET /api/csrf.

    Raises :class:`AuthError` if the endpoint is unreachable or the token is
    absent from the response.
    """
    resp = await client.get(f"{base_url}/api/csrf")
    if resp.status_code not in (200, 204):
        raise AuthError(f"CSRF fetch failed with status {resp.status_code}")
    token = _extract_csrf(resp)
    if not token:
        raise AuthError("CSRF token not found in response headers after GET /api/csrf")
    return token


async def login(
    client: httpx.AsyncClient,
    base_url: str,
    username: str,
    password: str,
) -> str:
    """Login and return the CSRF token.

    If the login response includes ``X-CSRF-Token`` directly, that value is
    returned.  Otherwise :func:`fetch_csrf` is called to retrieve it.

    Raises:
        RateLimitError: on HTTP 429.
        AuthError: on any other non-2xx status.
    """
    resp = await client.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
    )
    if resp.status_code == 429:
        raise RateLimitError("Login rate-limited (429). Wait before retrying.")
    if resp.status_code not in (200, 204):
        raise AuthError(f"Login failed with status {resp.status_code}")
    csrf = _extract_csrf(resp)
    if not csrf:
        csrf = await fetch_csrf(client, base_url)
    return csrf


async def refresh_session(client: httpx.AsyncClient, base_url: str) -> str:
    """Refresh the session cookie and return a fresh CSRF token.

    Per spec: POST /api/auth/refresh, then always re-fetch X-CSRF-Token —
    the token does not survive the refresh.

    Raises :class:`AuthError` on non-2xx from the refresh endpoint.
    """
    resp = await client.post(f"{base_url}/api/auth/refresh")
    if resp.status_code not in (200, 204):
        raise AuthError(f"Session refresh failed with status {resp.status_code}")
    return await fetch_csrf(client, base_url)
