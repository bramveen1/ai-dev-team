"""Slack-driven credential management for the ``browser_use`` pack.

The locked-in design for issue #147 mirrors the github-pack
``grant <agent> github`` PAT flow but at a per-profile / per-credential
granularity. Bram DMs any agent:

    grant <profile> credentials <credential_key>
    revoke <profile> credentials <credential_key>

The router does **not** persist the credentials anywhere — they
travel one hop from Slack → router → sidecar's ``/api/add_credential``
endpoint. The sidecar encrypts on receipt and writes
``/config/browser_profiles/<profile>/credentials.age``. The router's
job here is exactly three things:

1. Refuse anything that isn't a Slack DM (channel id prefix ``D``).
2. Prompt twice (username, password) and forward to the sidecar.
3. Confirm with the credential_key only — never echo the password.

We import the sidecar client lazily so the router can boot without
the pack's helpers being importable (e.g. a stripped-down dev image).
Anything that goes wrong with the sidecar call is reported back to
the user in Slack; nothing is logged from the prompt replies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Slack DM channels always start with "D" (the channel id, not the
# channel name). We refuse public/group channels for the credential
# flow — locked decision #3 explicitly carves out the at-rest-in-Slack
# threat to "the same delete-the-message dance Bram already does for
# PATs", not "broadcasts to a public room".
_DM_CHANNEL_PREFIX = "D"

# Default sidecar URL — kept in sync with ``helpers/sidecar_client``'s
# constant. Override in tests via the ``sidecar_url`` kwarg.
DEFAULT_SIDECAR_URL = "http://browser-use:8080"


@dataclass
class CredentialGrantCommand:
    """One ``grant <profile> credentials <credential_key>`` request."""

    profile: str
    credential_key: str


@dataclass
class CredentialRevokeCommand:
    """One ``revoke <profile> credentials <credential_key>`` request."""

    profile: str
    credential_key: str


# Allow the same name shapes the sidecar validates (lowercase alnum +
# dashes + underscores, 1–64 chars). Doing the check at command-parse
# time means a typo gets a clean "no match" rather than a sidecar 400.
_NAME_RE = r"[a-z0-9][a-z0-9_-]{0,63}"
_GRANT_CREDS_RE = re.compile(
    rf"^grant\s+(?P<profile>{_NAME_RE})\s+credentials\s+(?P<key>{_NAME_RE})\s*$",
    re.IGNORECASE,
)
_REVOKE_CREDS_RE = re.compile(
    rf"^revoke\s+(?P<profile>{_NAME_RE})\s+credentials\s+(?P<key>{_NAME_RE})\s*$",
    re.IGNORECASE,
)


def parse_credential_command(text: str) -> CredentialGrantCommand | CredentialRevokeCommand | None:
    """Parse a ``grant/revoke ... credentials ...`` line. Returns None if not one."""
    stripped = (text or "").strip()
    if m := _GRANT_CREDS_RE.match(stripped):
        return CredentialGrantCommand(profile=m["profile"].lower(), credential_key=m["key"].lower())
    if m := _REVOKE_CREDS_RE.match(stripped):
        return CredentialRevokeCommand(profile=m["profile"].lower(), credential_key=m["key"].lower())
    return None


def _is_dm_channel(channel: str | None) -> bool:
    return isinstance(channel, str) and channel.startswith(_DM_CHANNEL_PREFIX)


async def _sidecar_post(
    path: str,
    payload: dict,
    *,
    sidecar_url: str,
    timeout: float = 30.0,
) -> dict:
    """POST to the sidecar and return its parsed JSON body.

    The payload is passed through verbatim; the router does NOT log it.
    Any exception is re-raised as :class:`RuntimeError` with the bare
    error type / status code so the Slack reply doesn't leak the
    payload. We deliberately avoid ``exc_info=True`` on the log call
    for the same reason — a traceback could embed the payload dict.
    """
    # Local import keeps httpx out of the import-time graph for hosts
    # that don't run the browser-use pack at all.
    import httpx

    base = sidecar_url.rstrip("/")
    url = f"{base}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"could not reach the browser-use sidecar at {base}. "
            "Start it with `docker compose --profile browser up -d browser-use`."
        ) from e
    except httpx.TimeoutException as e:
        raise RuntimeError(f"browser-use sidecar did not respond within {timeout:.0f}s") from e

    if response.status_code >= 400:
        # The sidecar's error bodies are crafted to never include the
        # payload material (see ``server.py``'s ``add_credential``
        # endpoint), so this is safe to surface to the user.
        try:
            detail = response.json().get("detail") or response.text
        except ValueError:
            detail = response.text or f"HTTP {response.status_code}"
        raise RuntimeError(f"sidecar returned {response.status_code}: {detail}")

    try:
        return response.json()
    except ValueError as e:
        raise RuntimeError("sidecar returned non-JSON body") from e


async def handle_credential_grant(
    cmd: CredentialGrantCommand,
    prompt,
    *,
    channel: str | None,
    sidecar_url: str | None = None,
) -> None:
    """Drive the two-prompt grant flow and POST the credential to the sidecar.

    ``prompt`` is a :class:`router.packs.grants.SlackPrompt`. We rely
    on its ``prompt(text)`` method to post a question and await the
    user's next thread reply. Each prompt is awaited separately so the
    user can paste the username, then the password, with time in
    between (and delete each message right after).
    """
    if not _is_dm_channel(channel):
        await prompt(
            ":lock: `grant <profile> credentials …` only works in a DM with the bot. "
            "Open a direct message and try again."
        )
        return

    base = sidecar_url or DEFAULT_SIDECAR_URL

    username = await prompt.prompt(
        f":closed_lock_with_key: Setting up credential `{cmd.credential_key}` for profile `{cmd.profile}`.\n"
        "Paste the *username* as your next message in this DM. Delete the message right after.",
        timeout=600,
    )
    username = (username or "").strip()
    if not username:
        await prompt(":x: Empty username — aborting. No data sent to the sidecar.")
        return

    password = await prompt.prompt(
        ":key: Got the username. Now paste the *password* as your next message. Delete it right after.",
        timeout=600,
    )
    password = (password or "").strip()
    if not password:
        await prompt(":x: Empty password — aborting. No data sent to the sidecar.")
        return

    try:
        result = await _sidecar_post(
            "/api/add_credential",
            {
                "profile": cmd.profile,
                "credential_key": cmd.credential_key,
                "username": username,
                "password": password,
            },
            sidecar_url=base,
        )
    except RuntimeError as e:
        # Important: never include the username/password in the reply.
        # The exception path here only references the sidecar's error
        # body, which is constructed to not echo the input.
        await prompt(f":x: Credential store failed: {e}")
        return
    finally:
        # Explicit del to drop the references promptly. CPython will
        # GC them anyway, but being explicit shortens the window in
        # which a tracker could observe them.
        del username
        del password

    confirmed_key = result.get("credential_key") or cmd.credential_key
    await prompt(
        f":white_check_mark: Credential `{confirmed_key}` stored for profile `{cmd.profile}`. "
        "Delete the two messages you sent — Slack still has them in DM history."
    )


async def handle_credential_revoke(
    cmd: CredentialRevokeCommand,
    prompt,
    *,
    channel: str | None,
    sidecar_url: str | None = None,
) -> None:
    """Remove one credential from the profile's blob via the sidecar.

    Other keys in the same blob are preserved by the sidecar
    (acceptance criterion: ``revoke`` removes the named key but leaves
    other credentials intact).
    """
    if not _is_dm_channel(channel):
        await prompt(":lock: `revoke <profile> credentials …` only works in a DM with the bot.")
        return

    base = sidecar_url or DEFAULT_SIDECAR_URL
    try:
        result = await _sidecar_post(
            "/api/revoke_credential",
            {"profile": cmd.profile, "credential_key": cmd.credential_key},
            sidecar_url=base,
        )
    except RuntimeError as e:
        await prompt(f":x: Credential revoke failed: {e}")
        return

    if result.get("removed"):
        await prompt(f":white_check_mark: Credential `{cmd.credential_key}` removed from profile `{cmd.profile}`.")
    else:
        await prompt(
            f":information_source: Credential `{cmd.credential_key}` was not present for profile `{cmd.profile}`; "
            "no change."
        )
