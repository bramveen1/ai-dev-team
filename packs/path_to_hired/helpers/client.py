"""Stateful HTTP client for the Path to Hired API.

Manages the login session, CSRF token, and cookie jar.  Transparently
retries on:

- 401 (session expired): calls :func:`~.auth.refresh_session`, re-fetches
  ``X-CSRF-Token``, then retries the original request once.
- 403 with a CSRF-related error body: re-fetches ``X-CSRF-Token`` via
  ``GET /api/csrf`` and retries once.

Instantiate as an async context manager::

    async with PathToHiredClient(credentials) as client:
        resp = await client.get("/api/admin/users")
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .auth import CSRF_HEADER, fetch_csrf, login, refresh_session
from .secrets import Credentials

logger = logging.getLogger(__name__)


def _is_csrf_error(resp: httpx.Response) -> bool:
    """Return True if the 403 response body indicates a CSRF problem."""
    try:
        body = resp.json()
        msg = str(body.get("message", "") or body.get("error", "")).lower()
        return "csrf" in msg
    except Exception:
        return "csrf" in resp.text.lower()


class PathToHiredClient:
    """Cookie-persisting HTTP client that handles auth retries."""

    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._base_url = credentials.base_url
        self._csrf_token: str | None = None
        self._http = httpx.AsyncClient(follow_redirects=True)

    async def __aenter__(self) -> "PathToHiredClient":
        await self._start_session()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._http.aclose()

    async def _start_session(self) -> None:
        self._csrf_token = await login(
            self._http,
            self._base_url,
            self._credentials.username,
            self._credentials.password,
        )

    def _csrf_headers(self) -> dict[str, str]:
        if self._csrf_token:
            return {CSRF_HEADER: self._csrf_token}
        return {}

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an authenticated request with automatic retry on auth errors."""
        url = f"{self._base_url}{path}"
        extra_headers: dict[str, str] = kwargs.pop("headers", {})

        headers = {**self._csrf_headers(), **extra_headers}
        resp = await self._http.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:
            logger.debug("401 on %s %s; refreshing session", method, path)
            self._csrf_token = await refresh_session(self._http, self._base_url)
            headers = {**self._csrf_headers(), **extra_headers}
            resp = await self._http.request(method, url, headers=headers, **kwargs)

        elif resp.status_code == 403 and _is_csrf_error(resp):
            logger.debug("403 CSRF error on %s %s; re-fetching token", method, path)
            self._csrf_token = await fetch_csrf(self._http, self._base_url)
            headers = {**self._csrf_headers(), **extra_headers}
            resp = await self._http.request(method, url, headers=headers, **kwargs)

        return resp

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)
