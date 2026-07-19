"""Optional: walk the user through obtaining the pack's secret(s).

The grant flow imports this module and calls ``acquire(say)`` with an
:class:`router.packs.grants.InputPrompt` — a ``say`` callable whose
``prompt()`` method collects a secret via the transport-neutral
``InputRequest`` primitive. ``acquire`` returns a dict to store in
``data/secrets.json`` under the pack's name. Keys in the dict should match
the pack's ``needs:`` list (or be a superset — extra keys are ignored at
dispatch time).

Patterns
--------

1. **Paste-a-token** — simplest. The grant handler can prompt directly; this
   module isn't even needed. Skip the file and let the Slack flow ask for a
   PAT.

2. **Device-code flow** — use ``router.packs.oauth_devicecode`` to drive
   the standard OAuth2 device-code flow. See ``packs/github/`` (added in
   PR 4) for a concrete example.

3. **Custom** — anything else. The only contract is: ``acquire(say) -> dict``.

Example skeleton:

```python
import os
from router.packs.oauth_devicecode import start_device_code, poll_for_token

DEVICE_CODE_URL = "https://example.com/oauth/device/code"
TOKEN_URL = "https://example.com/oauth/token"


async def acquire(say) -> dict:
    init = await start_device_code(
        device_code_url=DEVICE_CODE_URL,
        client_id=os.environ["EXAMPLE_CLIENT_ID"],
        scope="read write",
    )
    await say(
        f"Visit {init['verification_uri']} and enter code "
        f"`{init['user_code']}`. I'll wait."
    )
    tokens = await poll_for_token(
        token_url=TOKEN_URL,
        client_id=os.environ["EXAMPLE_CLIENT_ID"],
        device_code=init["device_code"],
        interval=init.get("interval", 5),
    )
    return {
        "EXAMPLE_TOKEN": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
    }
```
"""

# This template intentionally has no `acquire` function — copy this file
# into your pack and implement one.
