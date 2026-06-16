"""GitHub App installation-token minting helper.

Mints short-lived installation tokens from age-encrypted RSA private keys.
Uses the GitHub App JWT flow: sign an RS256 JWT with the App's private key,
then exchange it for a short-lived installation access token.

Entry point: :func:`mint_installation_token`.

Feature-gated via the ``USE_GITHUB_APP`` environment variable.  Callers
should check :func:`is_enabled` before using this module so the existing
PAT path is preserved when the flag is off.

Config shape expected in ``data/secrets.json``::

    {
      "github_app": {
        "dispatch": {"app_id": "123456", "installation_id": "78901234"},
        "review":   {"app_id": "234567", "installation_id": "89012345"}
      }
    }

PEM private keys live on disk at::

    /config/secrets/github-app/aidt-dispatch.pem.age
    /config/secrets/github-app/aidt-review.pem.age

The files are age-encrypted; this module decrypts them via the ``age`` CLI
at mint time so the plaintext key is never written to disk.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

import httpx
import jwt as pyjwt

from router.packs.secret_store import SecretStore

logger = logging.getLogger(__name__)

Role = Literal["dispatch", "review"]

_PEM_FILENAMES: dict[str, str] = {
    "dispatch": "aidt-dispatch.pem.age",
    "review": "aidt-review.pem.age",
}

_DEFAULT_KEYS_DIR = Path("/config/secrets/github-app")
_GITHUB_API_BASE = "https://api.github.com"


def is_enabled() -> bool:
    """Return True when the USE_GITHUB_APP feature flag is active."""
    return os.environ.get("USE_GITHUB_APP", "").lower() in ("1", "true", "yes")


def _read_role_config(role: Role, store: SecretStore) -> tuple[str, str]:
    """Return ``(app_id, installation_id)`` for *role* from the secret store.

    Raises :exc:`RuntimeError` if the config is absent or incomplete.
    """
    cfg = store.get("github_app")
    role_cfg = cfg.get(role) if isinstance(cfg, dict) else None
    if not role_cfg:
        raise RuntimeError(f"github_app.{role} not configured in secrets.json")
    app_id = role_cfg.get("app_id")
    installation_id = role_cfg.get("installation_id")
    if not app_id:
        raise RuntimeError(f"github_app.{role}.app_id missing in secrets.json")
    if not installation_id:
        raise RuntimeError(f"github_app.{role}.installation_id missing in secrets.json")
    return str(app_id), str(installation_id)


def _decrypt_pem(pem_age_path: Path) -> bytes:
    """Decrypt an age-encrypted PEM private key file.

    Requires the ``age`` CLI to be on PATH.  Raises :exc:`RuntimeError` if
    age is missing, times out, or exits non-zero.
    """
    try:
        result = subprocess.run(
            ["age", "--decrypt", str(pem_age_path)],
            capture_output=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("age CLI not found — install age in the router image") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("age --decrypt timed out") from exc
    if result.returncode != 0:
        raise RuntimeError(f"age --decrypt failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}")
    return result.stdout


def _make_jwt(app_id: str, pem_bytes: bytes) -> str:
    """Sign and return a GitHub App JWT (RS256) valid for 10 minutes.

    ``iat`` is backdated 60 s to tolerate minor clock skew between the
    router and GitHub's servers.
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 600,  # 10 minutes (GitHub maximum)
        "iss": app_id,
    }
    return pyjwt.encode(payload, pem_bytes, algorithm="RS256")


def _exchange_jwt_for_token(installation_id: str, app_jwt: str) -> str:
    """POST to GitHub to exchange an App JWT for an installation access token.

    Raises :exc:`RuntimeError` on any non-201 response or missing token body.
    """
    url = f"{_GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    with httpx.Client() as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15.0,
        )
    if resp.status_code != 201:
        raise RuntimeError(f"GitHub installation token mint failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError("GitHub API returned 201 but no token in response body")
    return token


def mint_installation_token(
    role: Role,
    *,
    secret_store: SecretStore | None = None,
    keys_dir: Path | None = None,
) -> str:
    """Mint a GitHub App installation token for *role* (``dispatch`` or ``review``).

    Steps:

    1. Read ``app_id`` + ``installation_id`` from ``secrets.json`` under
       ``github_app.<role>``.
    2. Decrypt the age-encrypted PEM from *keys_dir*
       (default: ``/config/secrets/github-app/``).
    3. Sign an RS256 JWT with the decrypted key.
    4. Exchange the JWT for a short-lived installation access token via the
       GitHub API.

    Raises :exc:`RuntimeError` on any failure.  Callers **must not** fall
    through to a cached PAT on error — surface the failure immediately
    (per #257 no-silent-fallback policy).
    """
    store = secret_store or SecretStore()
    app_id, installation_id = _read_role_config(role, store)

    keys = keys_dir or _DEFAULT_KEYS_DIR
    pem_filename = _PEM_FILENAMES[role]
    pem_age_path = keys / pem_filename
    if not pem_age_path.exists():
        raise RuntimeError(f"PEM key file not found: {pem_age_path}")

    pem_bytes = _decrypt_pem(pem_age_path)
    app_jwt = _make_jwt(app_id, pem_bytes)
    return _exchange_jwt_for_token(installation_id, app_jwt)
