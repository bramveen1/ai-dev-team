"""Age-encrypted secret bundle for the browser_use pack.

Secrets (cookies, saved passphrases, OAuth refresh tokens) live as
``*.age`` blobs under ``/config/secrets/browser/`` and are decrypted at
session start using a host-resident keyfile. The keyfile is the trust
root: anyone with read access to ``/etc/ai-dev-team/age.key`` can
decrypt every stored browser secret. The threat model in the pack
README states this explicitly.

This module is deliberately thin:

- Verify the keyfile exists and has restrictive permissions (0400 / 0600,
  not world-readable).
- Shell out to the ``age`` binary to decrypt one blob at a time.
- Hand the decrypted values back to the caller as a dict so the handler
  can inject them as env vars on the sidecar request. The plaintext is
  **never** written to disk.
- Track every decrypted value so logs / responses can be scrubbed.

The age binary lives in the sidecar image. On the host (for the bootstrap
script) we assume the operator has installed ``age`` via their package
manager.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_KEYFILE = Path("/etc/ai-dev-team/age.key")
DEFAULT_BUNDLE_DIR = Path("/config/secrets/browser")
KEYFILE_ENV = "BROWSER_USE_AGE_KEYFILE"
BUNDLE_DIR_ENV = "BROWSER_USE_SECRETS_DIR"

# Maximum allowed permission bits on the keyfile. Anything that grants
# read access to "group" or "other" is rejected — leaking a 0644 keyfile
# would compromise every encrypted browser secret on disk.
_KEYFILE_FORBIDDEN_BITS = 0o077

REDACTED = "[REDACTED]"


class SecretError(RuntimeError):
    """Raised when secret bootstrap or decryption fails.

    Distinct exception type so the handler can refuse to start the
    session cleanly without burying the cause under a generic
    ``RuntimeError``.
    """


@dataclass
class SecretBundle:
    """Decrypted secrets for one browser session.

    ``values`` is the keyword-only payload the handler hands to the
    sidecar. ``sources`` records which ``.age`` file each key came from
    so a missing-secret error can point at the right blob.
    """

    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def scrub(self, text: str) -> str:
        """Return ``text`` with every secret value replaced by ``[REDACTED]``.

        Used by the handler before logging anything. Empty / one-char
        values are skipped to avoid mangling unrelated text.
        """
        out = text
        for value in self.values.values():
            if not value or len(value) < 4:
                continue
            out = out.replace(value, REDACTED)
        return out


def resolve_keyfile(override: Path | str | None = None) -> Path:
    """Return the host keyfile path (override > env > default)."""
    if override is not None:
        return Path(override)
    env_value = os.environ.get(KEYFILE_ENV)
    if env_value:
        return Path(env_value)
    return DEFAULT_KEYFILE


def resolve_bundle_dir(override: Path | str | None = None) -> Path:
    """Return the encrypted-bundle dir path (override > env > default)."""
    if override is not None:
        return Path(override)
    env_value = os.environ.get(BUNDLE_DIR_ENV)
    if env_value:
        return Path(env_value)
    return DEFAULT_BUNDLE_DIR


def assert_keyfile_safe(keyfile: Path) -> None:
    """Raise :class:`SecretError` if the keyfile is missing or world-readable.

    Acceptance criterion: "Pack handler refuses to start if the host
    keyfile is missing or world-readable." This is the single check
    every entry point should call before doing anything secret-related.
    """
    if not keyfile.exists():
        raise SecretError(
            f"age keyfile not found at {keyfile}. Run scripts/bootstrap-browser-secrets.sh on the host first."
        )
    if not keyfile.is_file():
        raise SecretError(f"age keyfile at {keyfile} is not a regular file")

    mode = keyfile.stat().st_mode & 0o777
    if mode & _KEYFILE_FORBIDDEN_BITS:
        raise SecretError(
            f"age keyfile at {keyfile} has unsafe permissions {mode:#o}; "
            "expected 0400 or 0600. Fix with `chmod 0400 "
            f"{keyfile}`."
        )


def decrypt_blob(
    blob_path: Path,
    *,
    keyfile: Path | None = None,
    age_binary: str = "age",
) -> str:
    """Decrypt one ``.age`` file and return the plaintext.

    The plaintext is held in memory only — callers must not write it to
    disk or print it. Errors from the ``age`` binary surface as
    :class:`SecretError` with the binary's stderr attached for debug.
    """
    keyfile = keyfile if keyfile is not None else resolve_keyfile()
    assert_keyfile_safe(keyfile)

    if not blob_path.exists():
        raise SecretError(f"encrypted blob not found: {blob_path}")

    try:
        proc = subprocess.run(
            [age_binary, "--decrypt", "--identity", str(keyfile), str(blob_path)],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as e:
        raise SecretError(f"`{age_binary}` binary not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise SecretError(f"age decrypt timed out for {blob_path}") from e

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise SecretError(f"age decrypt failed for {blob_path}: {stderr}")
    return proc.stdout.decode("utf-8")


def load_bundle(
    profile: str,
    *,
    keyfile: Path | None = None,
    bundle_dir: Path | None = None,
    age_binary: str = "age",
) -> SecretBundle:
    """Decrypt all secrets for ``profile`` into an in-memory bundle.

    Looks for ``<bundle_dir>/<profile>.env.age`` — a simple
    ``KEY=value``-per-line file after decryption. If no blob exists for
    the profile, returns an empty bundle (some profiles need no stored
    secrets, only cookies which live in the profile dir).
    """
    bundle_dir = bundle_dir if bundle_dir is not None else resolve_bundle_dir()
    bundle = SecretBundle()

    blob_path = bundle_dir / f"{profile}.env.age"
    if not blob_path.exists():
        logger.debug("no encrypted bundle for profile %s at %s", profile, blob_path)
        return bundle

    plaintext = decrypt_blob(blob_path, keyfile=keyfile, age_binary=age_binary)
    for raw in plaintext.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("malformed line in %s (no '='); skipping", blob_path)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        bundle.values[key] = value
        bundle.sources[key] = str(blob_path)
    return bundle
