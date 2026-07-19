"""Brevo (Maton gateway) token acquisition for the brevo pack.

Triggered by ``grant <agent> brevo`` in Slack. Walks the user
through grabbing a Maton gateway API key, waits for them to paste it
into the same Slack thread, validates it via ``GET /brevo/v3/account``,
then returns it to the router for storage under
``data/secrets.json["brevo"]["BREVO_API_KEY"]``.

Why "BREVO_API_KEY" and not "MATON_API_KEY": the agent only ever sees
this as the bearer token to call Brevo. Naming it after the underlying
gateway leaks an implementation detail into the prompt.
"""

from __future__ import annotations

import httpx

from router.packs.grants import InputPrompt

MATON_DASHBOARD_URL = "https://ctrl.maton.ai"
ACCOUNT_URL = "https://gateway.maton.ai/brevo/v3/account"

PROMPT_MESSAGE = (
    f":envelope_with_arrow: Generate a Maton gateway API key at {MATON_DASHBOARD_URL}\n"
    "• Make sure the key is scoped to the `brevo` connection\n"
    "• Copy the full key — it's shown only once\n\n"
    "Paste the key as your *next message* in this thread. "
    "I'll validate it against the gateway and store it. You can delete "
    "the message right after."
)


async def acquire(say: InputPrompt) -> dict:
    """Prompt for, validate, and return the Brevo token for storage."""
    if not isinstance(say, InputPrompt):
        raise RuntimeError(
            "brevo pack requires an InputPrompt for the paste flow; got a "
            "plain say callable. Run grant from a chat surface rather than CLI."
        )

    raw = await say.prompt(PROMPT_MESSAGE, timeout=600)
    token = _strip_token(raw)

    if not token:
        raise RuntimeError("no token received — the message was empty after stripping")

    label = await _validate(token)
    await say(f":white_check_mark: Token validated against the gateway ({label}). Storing…")
    return {"BREVO_API_KEY": token}


def _strip_token(raw: str) -> str:
    """Remove whitespace and common Slack code-fence wrappers."""
    text = (raw or "").strip()
    if text.startswith("`") and text.endswith("`"):
        text = text.strip("`").strip()
    return text


async def _validate(token: str) -> str:
    """Call the account endpoint and return a human-readable account label.

    Brevo's ``GET /v3/account`` returns a JSON object describing the
    account (email, plan, company name, …). We surface a short label
    in the validation message so operators see *which* Brevo account
    the key resolves to.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            ACCOUNT_URL,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code == 401:
        raise RuntimeError(
            "Maton rejected the token (401 Unauthorized). Generate a fresh "
            f"one at {MATON_DASHBOARD_URL} and make sure it's scoped to "
            "the brevo connection."
        )
    if response.status_code != 200:
        raise RuntimeError(f"Maton returned {response.status_code} validating the token: {response.text[:200]}")

    payload = response.json()
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(
            "Maton returned an unexpected payload shape — token may be valid but the account is not readable"
        )

    email = payload.get("email")
    company = payload.get("companyName")
    if email and company:
        return f"{email} — {company}"
    if email:
        return str(email)
    if company:
        return str(company)
    return "account reachable"
