"""HTTP client for the ``browser-use`` sidecar.

The sidecar exposes a tiny JSON-over-HTTP API on the docker network at
``http://browser-use:8080``. The router doesn't speak this protocol —
only the pack handler does, so the surface is intentionally tiny:

- :meth:`SidecarClient.health` — cheap GET used by the dispatcher to
  verify the sidecar is reachable before starting a session.
- :meth:`SidecarClient.invoke` — POST a single action with its payload
  (URL, profile, selector map, …) and return the JSON response.

Network errors surface as :class:`SidecarUnreachable` rather than a
raw httpx exception, so the handler can produce a clean
"sidecar not running — start it with `docker compose --profile browser
up`" error instead of leaking a stack trace into the agent's view.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://browser-use:8080"
DEFAULT_TIMEOUT = 60.0
BASE_URL_ENV = "BROWSER_USE_SIDECAR_URL"
TIMEOUT_ENV = "BROWSER_USE_SIDECAR_TIMEOUT"


class SidecarError(RuntimeError):
    """Base class for sidecar-related failures."""


class SidecarUnreachable(SidecarError):
    """Sidecar refused the connection or did not respond in time.

    Distinct from a 4xx/5xx — the dispatcher uses this to decide
    whether to retry with a startup hint to the operator.
    """


class SidecarBadResponse(SidecarError):
    """Sidecar returned a non-2xx status or an unparseable body."""


def resolve_base_url(override: str | None = None) -> str:
    if override is not None:
        return override
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL)


def resolve_timeout(override: float | None = None) -> float:
    if override is not None:
        return override
    raw = os.environ.get(TIMEOUT_ENV)
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r; falling back to %.1fs", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT


@dataclass
class SidecarResponse:
    """Successful sidecar response — status code plus parsed JSON body."""

    status: int
    body: dict[str, Any]


class SidecarClient:
    """Synchronous httpx-backed client for the browser-use sidecar.

    Synchronous on purpose: the pack handler is a one-shot CLI invoked
    by the agent's Bash tool, not a long-lived async server. Keeping
    the client sync removes one layer of complexity.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = resolve_base_url(base_url).rstrip("/")
        self.timeout = resolve_timeout(timeout)
        self._owned_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> SidecarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> SidecarResponse:
        """Probe ``GET /health``. Raises :class:`SidecarUnreachable` on connect errors."""
        return self._request("GET", "/health", json=None)

    def invoke(self, action: str, payload: dict[str, Any]) -> SidecarResponse:
        """Send a single browser action to the sidecar.

        ``action`` is the verb (``navigate``, ``extract``, …) and
        ``payload`` carries the action-specific arguments plus the
        ``profile`` name. Decrypted secrets go in ``payload["env"]``
        and are passed by reference once; the sidecar never echoes
        them back.
        """
        return self._request("POST", f"/api/{action}", json=payload)

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None) -> SidecarResponse:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, url, json=json)
        except httpx.ConnectError as e:
            raise SidecarUnreachable(
                f"could not connect to browser-use sidecar at {self.base_url}. "
                "Start it with `docker compose --profile browser up -d browser-use`."
            ) from e
        except httpx.TimeoutException as e:
            raise SidecarUnreachable(
                f"browser-use sidecar at {self.base_url} did not respond within {self.timeout:.0f}s"
            ) from e

        if response.status_code >= 500:
            raise SidecarBadResponse(
                f"sidecar returned {response.status_code} for {method} {path}: {response.text[:200]}"
            )
        try:
            body = response.json() if response.content else {}
        except ValueError as e:
            raise SidecarBadResponse(
                f"sidecar returned non-JSON body for {method} {path}: {response.text[:200]}"
            ) from e
        if not isinstance(body, dict):
            raise SidecarBadResponse(f"sidecar returned non-object JSON for {method} {path}: {body!r}")
        return SidecarResponse(status=response.status_code, body=body)
