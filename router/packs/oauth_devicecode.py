"""Generic OAuth2 device-code helper, agnostic to provider.

Extracted from the old ``capabilities/oauth.py`` (Microsoft-flavored) and
flattened: provider-specific URL templates are now passed in, not hardcoded.
A pack's ``authenticate.py`` typically calls :func:`start_device_code` and
:func:`poll_for_token` directly, then stores the resulting credentials via
:class:`router.packs.secret_store.SecretStore`.

Pack flow:

```python
# packs/github/authenticate.py
from router.packs.oauth_devicecode import start_device_code, poll_for_token

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"

async def acquire(say) -> dict:
    initiation = await start_device_code(
        device_code_url=DEVICE_CODE_URL,
        client_id=os.environ["GITHUB_CLIENT_ID"],
        scope="repo read:org",
    )
    await say(f"Visit {initiation['verification_uri']} and enter code {initiation['user_code']}.")
    return await poll_for_token(
        token_url=TOKEN_URL,
        client_id=os.environ["GITHUB_CLIENT_ID"],
        device_code=initiation["device_code"],
        interval=initiation["interval"],
    )
```
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OAuthError(Exception):
    """Raised when an OAuth device-code flow fails."""


async def start_device_code(
    device_code_url: str,
    client_id: str,
    scope: str,
    extra_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Initiate the OAuth2 device-code flow.

    Returns the provider's response, typically containing keys like
    ``user_code``, ``verification_uri``, ``device_code``, ``expires_in``,
    ``interval``. Pack authors pass these on to the user via Slack and then
    call :func:`poll_for_token`.

    Args:
        device_code_url: Provider's device-code endpoint (already substituted
            for tenant or similar).
        client_id: OAuth client ID.
        scope: Space-separated OAuth scopes.
        extra_params: Additional form fields some providers require (e.g.
            ``client_secret`` for confidential clients).

    Raises:
        OAuthError: If the request fails.
    """
    data: dict[str, str] = {"client_id": client_id, "scope": scope}
    if extra_params:
        data.update(extra_params)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            device_code_url,
            data=data,
            headers={"Accept": "application/json"},
        )

    if response.status_code != 200:
        raise OAuthError(f"device-code request failed ({response.status_code}): {response.text}")

    return response.json()


async def poll_for_token(
    token_url: str,
    client_id: str,
    device_code: str,
    interval: int = 5,
    timeout: int = 300,
    extra_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Poll the token endpoint until the user completes device-code auth.

    Returns the provider's token response, typically including ``access_token``
    plus optional ``refresh_token``, ``expires_in``, ``token_type``, ``scope``.

    Args:
        token_url: Provider's token endpoint.
        client_id: OAuth client ID.
        device_code: Device code from :func:`start_device_code`.
        interval: Polling interval in seconds (from the initiation response).
        timeout: Maximum seconds to wait before giving up.
        extra_params: Additional form fields some providers require.

    Raises:
        OAuthError: On user denial, code expiry, timeout, or unexpected error.
    """
    data: dict[str, str] = {
        "client_id": client_id,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }
    if extra_params:
        data.update(extra_params)

    deadline = time.monotonic() + timeout

    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            response = await client.post(
                token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
            try:
                result = response.json()
            except ValueError as e:
                raise OAuthError(f"non-JSON token response: {response.text}") from e

            if response.status_code == 200 and "access_token" in result:
                return result

            error = result.get("error", "")
            if error == "authorization_pending":
                await asyncio.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                await asyncio.sleep(interval)
                continue
            if error in ("authorization_declined", "access_denied"):
                raise OAuthError("user declined authorization")
            if error == "expired_token":
                raise OAuthError("device code expired before user completed auth")

            error_desc = result.get("error_description", response.text)
            raise OAuthError(f"token request failed: {error or response.status_code} — {error_desc}")

    raise OAuthError(f"polling timed out after {timeout}s")
