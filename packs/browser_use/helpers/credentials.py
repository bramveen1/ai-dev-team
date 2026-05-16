"""Per-profile, age-encrypted login credentials.

A second instance of the age-encrypted-secrets pattern (the first is the
``<profile>.env.age`` env-style bundle in :mod:`helpers.secrets`). This
one stores the structured ``{credential_key: {username, password}}``
map needed by the sidecar's ``login`` verb at
``<profile-dir>/credentials.age``.

Why a separate file from ``<profile>.env.age``:

- Different shape (JSON object, not ``KEY=value`` lines) so it can hold
  one entry per logical login (e.g. the same Bram-owned profile may
  hold creds for two different sites).
- Different lifecycle: env bundles are operator-managed via the host
  helper; credentials are operator-managed via the Slack grant flow
  (issue #147 decision #3) and rewritten on every ``add``/``remove``.
- Different threat surface: ``credentials.age`` lives *inside* the
  per-profile mode-0700 dir alongside ``cookies.json``; the env bundle
  lives in the global ``/config/secrets/browser/`` tree.

All ``add_credential`` / ``remove_credential`` calls re-encrypt the
whole file under the profile's age recipient. We deliberately do not
support partial / atomic-key updates: the file is small (handful of
keys at most) and "rewrite the whole thing" sidesteps merge bugs.

The plaintext is only ever held in memory inside the sidecar process.
This module **never** logs key/value contents — only the credential_key
name, never the username or password. Callers must follow the same
rule; ``server.py`` enforces it for the HTTP layer.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from helpers.secrets import (
    SecretError,
    assert_keyfile_safe,
    decrypt_blob,
    resolve_keyfile,
)

logger = logging.getLogger(__name__)

CREDENTIALS_FILENAME = "credentials.age"
CREDENTIALS_MODE = 0o600

# Public-key extraction. age-keygen writes the recipient on a comment
# line of the form ``# public key: age1...``. We parse the keyfile
# rather than asking the operator to copy the pubkey into a separate
# config — keeps the trust root in one place.
_PUBKEY_PREFIX = "# public key:"


class CredentialError(RuntimeError):
    """Raised when credential encryption / decryption / storage fails."""


@dataclass(frozen=True)
class Credential:
    """A single decrypted credential entry. ``__repr__`` masks the password."""

    username: str
    password: str

    def __repr__(self) -> str:
        # Defensive: a stray ``repr(cred)`` in a log line must not leak
        # the password. The username is not a secret per se but we mask
        # it for symmetry — log the credential_key instead.
        return "Credential(username=<redacted>, password=<redacted>)"


def extract_public_key(keyfile: Path) -> str:
    """Return the age recipient (``age1...``) embedded in ``keyfile``.

    The keyfile is the trust root and must already pass
    :func:`helpers.secrets.assert_keyfile_safe`; we re-check here so a
    bad-permissions keyfile can't silently leak its public key into a
    logged subprocess argv.
    """
    assert_keyfile_safe(keyfile)
    try:
        text = keyfile.read_text()
    except OSError as e:
        raise CredentialError(f"could not read age keyfile at {keyfile}: {e}") from e
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_PUBKEY_PREFIX):
            pubkey = stripped[len(_PUBKEY_PREFIX) :].strip()
            if pubkey.startswith("age1"):
                return pubkey
            raise CredentialError(
                f"keyfile {keyfile} '{_PUBKEY_PREFIX}' line is malformed: expected an 'age1...' recipient"
            )
    raise CredentialError(f"keyfile {keyfile} has no '{_PUBKEY_PREFIX}' comment line; cannot derive recipient")


def _credentials_path(profile_path: Path) -> Path:
    return profile_path / CREDENTIALS_FILENAME


def _encrypt_to_path(
    payload: dict[str, dict[str, str]],
    output_path: Path,
    *,
    keyfile: Path,
    age_binary: str = "age",
) -> None:
    """Encrypt ``payload`` (JSON-serialised) to ``output_path`` via stdin.

    Uses an explicit recipient (``-r ageX...``) extracted from the
    keyfile so the plaintext never touches a temp file on disk —
    everything goes through age's stdin and the resulting ciphertext is
    written atomically via ``output_path.tmp`` + ``replace``. The
    caller is responsible for the surrounding profile dir's mode 0700.
    """
    pubkey = extract_public_key(keyfile)
    plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        proc = subprocess.run(
            [age_binary, "--encrypt", "--recipient", pubkey, "--output", str(tmp_path)],
            input=plaintext,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as e:
        raise CredentialError(f"`{age_binary}` binary not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise CredentialError(f"age encrypt timed out for {output_path}") from e

    if proc.returncode != 0:
        # Clean up the partial temp file so we never leave half-written
        # ciphertext lying around.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CredentialError(f"age encrypt failed for {output_path}: {stderr}")

    # Force tight mode before we publish the file so a race-reading
    # process can't see a 0644 ciphertext (umask drift).
    try:
        os.chmod(tmp_path, CREDENTIALS_MODE)
    except OSError as e:
        raise CredentialError(f"could not chmod {tmp_path}: {e}") from e
    tmp_path.replace(output_path)


def load_credentials(
    profile_path: Path,
    *,
    keyfile: Path | None = None,
    age_binary: str = "age",
) -> dict[str, Credential]:
    """Decrypt the profile's ``credentials.age`` into ``{key: Credential}``.

    Returns an empty dict if no credentials file exists for the profile
    — that's the expected state for a profile that hasn't been wired up
    yet. Any other failure (keyfile bad, decrypt fails, file isn't
    valid JSON, JSON has the wrong shape) raises :class:`CredentialError`.
    """
    keyfile = keyfile if keyfile is not None else resolve_keyfile()
    blob = _credentials_path(profile_path)
    if not blob.exists():
        return {}
    try:
        plaintext = decrypt_blob(blob, keyfile=keyfile, age_binary=age_binary)
    except SecretError as e:
        raise CredentialError(f"could not decrypt {blob}: {e}") from e
    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError as e:
        raise CredentialError(f"{blob} did not decrypt to valid JSON: {e}") from e
    if not isinstance(decoded, dict):
        raise CredentialError(f"{blob} must decrypt to a JSON object, got {type(decoded).__name__}")

    out: dict[str, Credential] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or not key:
            raise CredentialError(f"{blob} contains a non-string credential key {key!r}")
        if not isinstance(value, dict):
            raise CredentialError(f"{blob} entry for {key!r} is not an object")
        username = value.get("username")
        password = value.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise CredentialError(f"{blob} entry for {key!r} must contain string 'username' and 'password' fields")
        out[key] = Credential(username=username, password=password)
    return out


def get_credential(
    profile_path: Path,
    credential_key: str,
    *,
    keyfile: Path | None = None,
    age_binary: str = "age",
) -> Credential:
    """Decrypt the file and return one credential by key.

    Raises :class:`CredentialError` if the file is missing or the key
    isn't present — the caller should surface that to the agent as a
    distinct error_type so the operator knows to run the grant flow.
    """
    creds = load_credentials(profile_path, keyfile=keyfile, age_binary=age_binary)
    if credential_key not in creds:
        raise CredentialError(
            f"credential_key {credential_key!r} not found in {_credentials_path(profile_path)}; "
            f"run `grant <profile> credentials {credential_key}` from Slack to add it"
        )
    return creds[credential_key]


def set_credential(
    profile_path: Path,
    credential_key: str,
    username: str,
    password: str,
    *,
    keyfile: Path | None = None,
    age_binary: str = "age",
) -> None:
    """Add or replace ``credential_key`` in the profile's credentials file.

    Re-encrypts the entire file. Other keys are preserved. The
    plaintext is built and discarded inside this function — it never
    lives in a tracking variable that could outlive the call.
    """
    if not isinstance(credential_key, str) or not credential_key:
        raise CredentialError("credential_key must be a non-empty string")
    if not isinstance(username, str) or not username:
        raise CredentialError("username must be a non-empty string")
    if not isinstance(password, str) or not password:
        raise CredentialError("password must be a non-empty string")

    keyfile = keyfile if keyfile is not None else resolve_keyfile()
    existing = load_credentials(profile_path, keyfile=keyfile, age_binary=age_binary)
    payload: dict[str, dict[str, str]] = {
        k: {"username": c.username, "password": c.password} for k, c in existing.items()
    }
    payload[credential_key] = {"username": username, "password": password}
    _encrypt_to_path(payload, _credentials_path(profile_path), keyfile=keyfile, age_binary=age_binary)
    # Log the credential_key only — never the username or password.
    logger.info("credential %r stored for profile at %s", credential_key, profile_path)


def remove_credential(
    profile_path: Path,
    credential_key: str,
    *,
    keyfile: Path | None = None,
    age_binary: str = "age",
) -> bool:
    """Remove one key from the credentials file. Returns True if it existed.

    If removing the last key, the on-disk file is deleted entirely so
    a profile with no credentials looks the same on disk as one that
    never had any.
    """
    keyfile = keyfile if keyfile is not None else resolve_keyfile()
    blob = _credentials_path(profile_path)
    if not blob.exists():
        return False
    existing = load_credentials(profile_path, keyfile=keyfile, age_binary=age_binary)
    if credential_key not in existing:
        return False
    del existing[credential_key]
    if not existing:
        blob.unlink()
        logger.info("removed last credential %r and deleted %s", credential_key, blob)
        return True
    payload = {k: {"username": c.username, "password": c.password} for k, c in existing.items()}
    _encrypt_to_path(payload, blob, keyfile=keyfile, age_binary=age_binary)
    logger.info("credential %r removed for profile at %s", credential_key, profile_path)
    return True
