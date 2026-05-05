"""GitHub OAuth device-code flow for the github pack.

Triggered by ``grant <agent> github`` in Slack. Walks the user through
the device-code flow and returns the access token, which the router
stores under ``data/secrets.json["github"]["GITHUB_TOKEN"]``.

Operator setup (one-time)
-------------------------

1. Register an OAuth App at https://github.com/settings/applications/new.
   - Application name: anything (e.g. "AI Dev Team Router").
   - Homepage URL: anything.
   - Authorization callback URL: anything (we don't use it for device flow).
2. After creation, open the app's settings and **enable Device Flow**
   (a checkbox in the app's General settings).
3. Copy the Client ID and add it to ``.env``:
   ``GITHUB_CLIENT_ID=Iv1.xxxxxxxxxxxxxxxx``
4. Restart the router: ``docker compose restart router``.

After that, ``grant <agent> github`` from Slack runs the rest.
"""

from __future__ import annotations

import os

from router.packs.oauth_devicecode import (
    poll_for_token,
    start_device_code,
)

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
SCOPES = "repo read:org"
VERIFICATION_URL = "https://github.com/login/device"


async def acquire(say) -> dict:
    """Run the device-code flow and return secrets to store."""
    client_id = os.environ.get("GITHUB_CLIENT_ID")
    if not client_id:
        raise RuntimeError(
            "GITHUB_CLIENT_ID is not set. Register a GitHub OAuth App "
            "(https://github.com/settings/applications/new), enable Device "
            "Flow on it, and add `GITHUB_CLIENT_ID=...` to `.env`. See "
            "packs/github/authenticate.py for full setup steps."
        )

    initiation = await start_device_code(
        device_code_url=DEVICE_CODE_URL,
        client_id=client_id,
        scope=SCOPES,
    )

    user_code = initiation["user_code"]
    await say(
        f":key: Visit {VERIFICATION_URL} and enter code `{user_code}`. I'll wait until you authorize (up to ~10 min)."
    )

    tokens = await poll_for_token(
        token_url=TOKEN_URL,
        client_id=client_id,
        device_code=initiation["device_code"],
        interval=initiation.get("interval", 5),
        timeout=600,
    )

    return {"GITHUB_TOKEN": tokens["access_token"]}
