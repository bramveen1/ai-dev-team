"""GitHub PAT acquisition for the github pack.

Triggered by ``grant <agent> github`` in Slack. Walks the user through
generating a Personal Access Token (PAT), waits for them to paste it
back into the same Slack thread, validates the token against the
GitHub API, then returns it to the router for storage under
``data/secrets.json["github"]["GITHUB_TOKEN"]``.

PATs over OAuth: chosen so operators don't have to register a GitHub
OAuth App, enable Device Flow, and add a client ID to ``.env``. The
trade-off is that the PAT briefly lives in a Slack DM message — the
user can delete the message right after pasting if they want.
"""

from __future__ import annotations

import httpx

from router.packs.grants import InputPrompt

PAT_DOC_URL = "https://github.com/settings/tokens/new"
USER_API_URL = "https://api.github.com/user"
RECOMMENDED_SCOPES = "repo, read:org"

PROMPT_MESSAGE = (
    f":key: Generate a GitHub PAT at {PAT_DOC_URL}\n"
    f"• Required scopes: `{RECOMMENDED_SCOPES}`\n"
    f"• Set an expiry that fits your security policy (90 days is fine)\n\n"
    "Paste the token as your *next message* in this thread. "
    "I'll validate it and store it. You can delete the message right after."
)


async def acquire(say: InputPrompt) -> dict:
    """Prompt for, validate, and return the PAT for storage."""
    if not isinstance(say, InputPrompt):
        raise RuntimeError(
            "github pack requires an InputPrompt for the PAT flow; got a plain "
            "say callable. Run grant from a chat surface rather than CLI."
        )

    raw = await say.prompt(PROMPT_MESSAGE, timeout=600)
    token = _strip_token(raw)

    if not token:
        raise RuntimeError("no token received — the message was empty after stripping")

    login = await _validate(token)
    await say(f":white_check_mark: Token validated for `@{login}`. Storing…")
    return {"GITHUB_TOKEN": token}


def _strip_token(raw: str) -> str:
    """Remove whitespace and common Slack code-fence wrappers."""
    text = (raw or "").strip()
    if text.startswith("`") and text.endswith("`"):
        text = text.strip("`").strip()
    return text


async def _validate(token: str) -> str:
    """Call ``GET /user`` and return the authenticated login (or raise)."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            USER_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if response.status_code == 401:
        raise RuntimeError(
            "GitHub rejected the token (401 Unauthorized). Generate a fresh "
            f"one at {PAT_DOC_URL} with the scopes `{RECOMMENDED_SCOPES}`."
        )
    if response.status_code != 200:
        raise RuntimeError(f"GitHub returned {response.status_code} validating the token: {response.text[:200]}")
    payload = response.json()
    login = payload.get("login")
    if not login:
        raise RuntimeError("GitHub returned no login field — token shape unexpected")
    return login
