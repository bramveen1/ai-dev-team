"""Zoho Mail (Maton gateway) token acquisition for the zoho-mail pack.

Triggered by ``grant <agent> zoho-mail`` in Slack. Walks the user
through grabbing a Maton gateway API key, waits for them to paste it
into the same Slack thread, validates it by listing accounts, then
returns it to the router for storage under
``data/secrets.json["zoho-mail"]["ZOHO_API_KEY"]``.

Why "ZOHO_API_KEY" and not "MATON_API_KEY": the agent only ever sees
this as the bearer token to call Zoho Mail. Naming it after the
underlying gateway leaks an implementation detail into the prompt.
"""

from __future__ import annotations

import httpx

from router.packs.grants import InputPrompt

MATON_DASHBOARD_URL = "https://ctrl.maton.ai"
ACCOUNTS_URL = "https://gateway.maton.ai/zoho-mail/api/accounts"

PROMPT_MESSAGE = (
    f":envelope_with_arrow: Generate a Maton gateway API key at {MATON_DASHBOARD_URL}\n"
    "• Make sure the key is scoped to the `zoho-mail` connection\n"
    "• Copy the full key — it's shown only once\n\n"
    "Paste the key as your *next message* in this thread. "
    "I'll validate it against the gateway and store it. You can delete "
    "the message right after."
)


async def acquire(say: InputPrompt) -> dict:
    """Prompt for, validate, and return the Zoho Mail token for storage."""
    if not isinstance(say, InputPrompt):
        raise RuntimeError(
            "zoho-mail pack requires an InputPrompt for the paste flow; got a "
            "plain say callable. Run grant from a chat surface rather than CLI."
        )

    raw = await say.prompt(PROMPT_MESSAGE, timeout=600)
    token = _strip_token(raw)

    if not token:
        raise RuntimeError("no token received — the message was empty after stripping")

    account_count = await _validate(token)
    accounts_summary = "1 account" if account_count == 1 else f"{account_count} accounts"
    await say(f":white_check_mark: Token validated against the gateway ({accounts_summary} reachable). Storing…")
    return {"ZOHO_API_KEY": token}


def _strip_token(raw: str) -> str:
    """Remove whitespace and common Slack code-fence wrappers."""
    text = (raw or "").strip()
    if text.startswith("`") and text.endswith("`"):
        text = text.strip("`").strip()
    return text


async def _validate(token: str) -> int:
    """Call the accounts endpoint and return the account count (or raise)."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            ACCOUNTS_URL,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code == 401:
        raise RuntimeError(
            "Maton rejected the token (401 Unauthorized). Generate a fresh "
            f"one at {MATON_DASHBOARD_URL} and make sure it's scoped to "
            "the zoho-mail connection."
        )
    if response.status_code != 200:
        raise RuntimeError(f"Maton returned {response.status_code} validating the token: {response.text[:200]}")

    payload = response.json()
    if isinstance(payload, list):
        accounts = payload
    elif isinstance(payload, dict) and "data" in payload:
        accounts = payload["data"]
    elif isinstance(payload, dict) and "accounts" in payload:
        accounts = payload["accounts"]
    else:
        raise RuntimeError(
            "Maton returned an unexpected payload shape — token may be valid but accounts are not listable"
        )

    if not isinstance(accounts, list):
        raise RuntimeError(
            "Maton returned an unexpected payload shape — token may be valid but accounts are not listable"
        )
    if not accounts:
        raise RuntimeError("Maton returned zero accounts — the token has no Zoho mailboxes attached")
    return len(accounts)
