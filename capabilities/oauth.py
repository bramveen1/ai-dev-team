"""OAuth2 device code flow and token refresh for provider authentication.

Provides reusable OAuth2 utilities for providers that use token-based auth
(e.g. Microsoft 365, Google). Supports:

- Device code flow: Headless authentication where the user visits a URL
  and enters a code. The agent can drive this interactively via Slack.
- Token refresh: Exchange a refresh_token for a new access_token.
- Auto-refresh: Check token expiry and refresh if needed before use.

All HTTP calls use httpx for async support. URLs are templated so the
same code works for M365, Google, and other OAuth2 providers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from capabilities.secrets import SecretStore

logger = logging.getLogger(__name__)

# Default Microsoft endpoints (used when oauth config doesn't specify)
M365_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
M365_DEVICE_CODE_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/devicecode"


class OAuthError(Exception):
    """Raised when an OAuth flow fails."""


async def start_device_code_flow(
    tenant_id: str,
    client_id: str,
    scopes: str,
    client_secret: str | None = None,
    device_code_url_template: str = M365_DEVICE_CODE_URL,
) -> dict[str, Any]:
    """Initiate the OAuth2 device code flow.

    Posts to the device code endpoint and returns the response containing
    the user code and verification URI.

    Args:
        tenant_id: Azure AD tenant ID.
        client_id: Application (client) ID.
        scopes: Space-separated OAuth scopes.
        client_secret: Optional client secret for confidential clients.
        device_code_url_template: URL template with ``{tenant_id}`` placeholder.

    Returns:
        Dict with keys: user_code, verification_uri, device_code,
        expires_in, interval, message.

    Raises:
        OAuthError: If the request fails.
    """
    url = device_code_url_template.format(tenant_id=tenant_id)
    data: dict[str, str] = {
        "client_id": client_id,
        "scope": scopes,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient() as client_http:
        response = await client_http.post(url, data=data)

    if response.status_code != 200:
        raise OAuthError(f"Device code request failed ({response.status_code}): {response.text}")

    return response.json()


async def poll_for_token(
    tenant_id: str,
    client_id: str,
    device_code: str,
    interval: int = 5,
    client_secret: str | None = None,
    token_url_template: str = M365_TOKEN_URL,
    timeout: int = 300,
) -> dict[str, Any]:
    """Poll the token endpoint until the user completes device code auth.

    Args:
        tenant_id: Azure AD tenant ID.
        client_id: Application (client) ID.
        device_code: Device code from start_device_code_flow().
        interval: Polling interval in seconds (from device code response).
        client_secret: Optional client secret.
        token_url_template: URL template with ``{tenant_id}`` placeholder.
        timeout: Maximum seconds to wait before giving up.

    Returns:
        Dict with keys: access_token, refresh_token, expires_in, token_type, scope.

    Raises:
        OAuthError: If the user denies, the code expires, or timeout is reached.
    """
    url = token_url_template.format(tenant_id=tenant_id)
    data: dict[str, str] = {
        "client_id": client_id,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }
    if client_secret:
        data["client_secret"] = client_secret

    deadline = time.monotonic() + timeout

    async with httpx.AsyncClient() as client_http:
        while time.monotonic() < deadline:
            response = await client_http.post(url, data=data)
            result = response.json()

            if response.status_code == 200:
                return result

            error = result.get("error", "")
            if error == "authorization_pending":
                await asyncio.sleep(interval)
                continue
            elif error == "slow_down":
                interval += 5
                await asyncio.sleep(interval)
                continue
            elif error in ("authorization_declined", "access_denied"):
                raise OAuthError("User declined authorization")
            elif error == "expired_token":
                raise OAuthError("Device code expired — user did not complete auth in time")
            else:
                error_desc = result.get("error_description", response.text)
                raise OAuthError(f"Token request failed: {error} — {error_desc}")

    raise OAuthError(f"Polling timed out after {timeout}s")


async def refresh_access_token(
    tenant_id: str,
    client_id: str,
    refresh_token: str,
    scopes: str,
    client_secret: str | None = None,
    token_url_template: str = M365_TOKEN_URL,
) -> dict[str, Any]:
    """Exchange a refresh token for a new access token.

    Args:
        tenant_id: Azure AD tenant ID.
        client_id: Application (client) ID.
        refresh_token: The refresh token to exchange.
        scopes: Space-separated OAuth scopes.
        client_secret: Optional client secret.
        token_url_template: URL template with ``{tenant_id}`` placeholder.

    Returns:
        Dict with keys: access_token, refresh_token (rotated),
        expires_in, token_type, scope.

    Raises:
        OAuthError: If the refresh fails (e.g. refresh token expired).
    """
    url = token_url_template.format(tenant_id=tenant_id)
    data: dict[str, str] = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scopes,
    }
    if client_secret:
        data["client_secret"] = client_secret

    async with httpx.AsyncClient() as client_http:
        response = await client_http.post(url, data=data)

    if response.status_code != 200:
        result = response.json()
        error = result.get("error", "unknown")
        error_desc = result.get("error_description", response.text)
        raise OAuthError(f"Token refresh failed: {error} — {error_desc}")

    return response.json()


async def ensure_valid_token(
    store: SecretStore,
    provider: str,
    oauth_config: dict[str, str],
) -> str:
    """Ensure a provider has a valid access token, refreshing if expired.

    Loads credentials from the secrets store, checks ``expires_at``,
    and refreshes the token if needed. Saves updated tokens back to the store.

    Args:
        store: SecretStore instance.
        provider: Provider name in the secrets store (e.g. "m365").
        oauth_config: OAuth config from providers.yaml with keys:
            authority, token_path, devicecode_path.

    Returns:
        A valid access token string.

    Raises:
        OAuthError: If refresh fails.
        SecretsError: If required credentials are missing from the store.
    """
    if not store.needs_refresh(provider):
        token = store.get(provider, "access_token")
        if token:
            return token

    # Load credentials for refresh
    secrets = store.load(provider)
    tenant_id = secrets.get("tenant_id", "")
    client_id = secrets.get("client_id", "")
    client_secret = secrets.get("client_secret")
    current_refresh_token = secrets.get("refresh_token", "")
    scopes = secrets.get("scopes", "")

    if not all([tenant_id, client_id, current_refresh_token]):
        raise OAuthError(
            f"Cannot refresh token for {provider}: missing tenant_id, client_id, or refresh_token. "
            "Run the device code flow to set up initial credentials."
        )

    # Build token URL from oauth config
    authority = oauth_config.get("authority", "https://login.microsoftonline.com")
    token_path = oauth_config.get("token_path", "/oauth2/v2.0/token")
    token_url = f"{authority}/{tenant_id}{token_path}"

    logger.info("Refreshing access token for provider %s", provider)

    result = await refresh_access_token(
        tenant_id=tenant_id,
        client_id=client_id,
        refresh_token=current_refresh_token,
        scopes=scopes,
        client_secret=client_secret,
        token_url_template=token_url.replace(tenant_id, "{tenant_id}"),
    )

    # Update secrets with new tokens
    secrets["access_token"] = result["access_token"]
    if "refresh_token" in result:
        secrets["refresh_token"] = result["refresh_token"]
    secrets["expires_at"] = int(time.time()) + int(result.get("expires_in", 3600))

    store.save(provider, secrets)
    logger.info("Token refreshed and saved for provider %s", provider)

    return result["access_token"]
