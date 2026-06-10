"""Age-encrypted credential loading for the path_to_hired pack.

Credentials are stored as a JSON blob in
``/config/secrets/path_to_hired/credentials.age``, age-encrypted with the
host keyfile at ``/etc/ai-dev-team/age.key``.  Decryption happens in-memory
only — plaintext is never written to disk.

If the credentials file is missing or unreadable, the pack surfaces a clean
:class:`CredentialError` with an actionable message rather than a traceback.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = Path("/config/secrets/path_to_hired/credentials.age")
DEFAULT_KEYFILE = Path("/etc/ai-dev-team/age.key")
CREDENTIALS_PATH_ENV = "PATH_TO_HIRED_CREDENTIALS"
KEYFILE_ENV = "PATH_TO_HIRED_AGE_KEYFILE"

_KEYFILE_FORBIDDEN_BITS = 0o077


class CredentialError(RuntimeError):
    """Raised when credential loading or decryption fails.

    Callers catch this and surface it as a structured error payload so the
    agent sees a clean message instead of a Python traceback.
    """


@dataclass(frozen=True)
class Credentials:
    base_url: str
    username: str
    password: str

    def __repr__(self) -> str:
        return f"Credentials(base_url={self.base_url!r}, username={self.username!r}, password='[REDACTED]')"


def resolve_credentials_path(override: Path | str | None = None) -> Path:
    """Return the credentials file path (override > env > default)."""
    if override is not None:
        return Path(override)
    env = os.environ.get(CREDENTIALS_PATH_ENV)
    if env:
        return Path(env)
    return DEFAULT_CREDENTIALS_PATH


def resolve_keyfile(override: Path | str | None = None) -> Path:
    """Return the age keyfile path (override > env > default)."""
    if override is not None:
        return Path(override)
    env = os.environ.get(KEYFILE_ENV)
    if env:
        return Path(env)
    return DEFAULT_KEYFILE


def _assert_keyfile_safe(keyfile: Path) -> None:
    if not keyfile.exists():
        raise CredentialError(f"age keyfile not found at {keyfile}")
    if not keyfile.is_file():
        raise CredentialError(f"age keyfile at {keyfile} is not a regular file")
    mode = keyfile.stat().st_mode & 0o777
    if mode & _KEYFILE_FORBIDDEN_BITS:
        raise CredentialError(f"age keyfile at {keyfile} has unsafe permissions {mode:#o}; expected 0400 or 0600")


def load_credentials(
    *,
    credentials_path: Path | None = None,
    keyfile: Path | None = None,
    age_binary: str = "age",
) -> Credentials:
    """Decrypt and return the Path to Hired service-account credentials.

    Raises :class:`CredentialError` if the credentials file is missing,
    the keyfile is absent/unsafe, decryption fails, or the decrypted JSON
    is missing required keys.  Never writes plaintext to disk.
    """
    creds_path = resolve_credentials_path(credentials_path)
    kf = resolve_keyfile(keyfile)

    if not creds_path.exists():
        raise CredentialError(
            f"credentials file not found at {creds_path}. "
            "Ensure /config/secrets/path_to_hired/credentials.age exists (mode 0600, owner uid 1000)."
        )

    _assert_keyfile_safe(kf)

    try:
        proc = subprocess.run(
            [age_binary, "--decrypt", "--identity", str(kf), str(creds_path)],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise CredentialError(f"`{age_binary}` binary not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(f"age decrypt timed out for {creds_path}") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CredentialError(f"age decrypt failed for {creds_path}: {stderr}")

    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CredentialError("credentials file is not valid JSON after decryption") from exc

    missing = [k for k in ("base_url", "username", "password") if k not in data]
    if missing:
        raise CredentialError(f"credentials JSON missing required keys: {missing}")

    return Credentials(
        base_url=data["base_url"].rstrip("/"),
        username=data["username"],
        password=data["password"],
    )
