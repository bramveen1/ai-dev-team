"""Tests for helpers/secrets.py — credential loading and age decryption."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers.secrets import CredentialError, Credentials, load_credentials

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_age_success(plaintext: str):
    """Patch subprocess.run to simulate a successful age decrypt."""
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = plaintext.encode()
    proc.stderr = b""
    return patch("helpers.secrets.subprocess.run", return_value=proc)


def _mock_age_failure(stderr: str = "bad identity"):
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = b""
    proc.stderr = stderr.encode()
    return patch("helpers.secrets.subprocess.run", return_value=proc)


def _valid_json() -> str:
    return json.dumps(
        {"base_url": "https://api.pathtohired.com", "username": "admin@example.com", "password": "s3cr3t"}
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    def test_happy_path(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake-age-blob")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"AGE-SECRET-KEY-1")
        keyfile.chmod(0o600)

        with _mock_age_success(_valid_json()):
            creds = load_credentials(credentials_path=creds_file, keyfile=keyfile)

        assert isinstance(creds, Credentials)
        assert creds.base_url == "https://api.pathtohired.com"
        assert creds.username == "admin@example.com"
        assert creds.password == "s3cr3t"

    def test_base_url_trailing_slash_stripped(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        payload = json.dumps({"base_url": "https://api.pathtohired.com/", "username": "u", "password": "p"})
        with _mock_age_success(payload):
            creds = load_credentials(credentials_path=creds_file, keyfile=keyfile)
        assert creds.base_url == "https://api.pathtohired.com"

    def test_missing_credentials_file(self, tmp_path: Path) -> None:
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        with pytest.raises(CredentialError, match="credentials file not found"):
            load_credentials(credentials_path=tmp_path / "nonexistent.age", keyfile=keyfile)

    def test_missing_keyfile(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")

        with pytest.raises(CredentialError, match="age keyfile not found"):
            load_credentials(credentials_path=creds_file, keyfile=tmp_path / "no.key")

    def test_keyfile_unsafe_permissions(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o644)

        with pytest.raises(CredentialError, match="unsafe permissions"):
            load_credentials(credentials_path=creds_file, keyfile=keyfile)

    def test_age_binary_not_found(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        with patch("helpers.secrets.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(CredentialError, match="not on PATH"):
                load_credentials(credentials_path=creds_file, keyfile=keyfile)

    def test_age_decrypt_timeout(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        with patch("helpers.secrets.subprocess.run", side_effect=subprocess.TimeoutExpired("age", 10)):
            with pytest.raises(CredentialError, match="timed out"):
                load_credentials(credentials_path=creds_file, keyfile=keyfile)

    def test_age_decrypt_failure(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        with _mock_age_failure("no matching recipients"):
            with pytest.raises(CredentialError, match="age decrypt failed"):
                load_credentials(credentials_path=creds_file, keyfile=keyfile)

    def test_invalid_json_after_decrypt(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        with _mock_age_success("not-json"):
            with pytest.raises(CredentialError, match="not valid JSON"):
                load_credentials(credentials_path=creds_file, keyfile=keyfile)

    def test_missing_required_keys(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        payload = json.dumps({"base_url": "https://api.example.com"})
        with _mock_age_success(payload):
            with pytest.raises(CredentialError, match="missing required keys"):
                load_credentials(credentials_path=creds_file, keyfile=keyfile)

    def test_repr_redacts_password(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.age"
        creds_file.write_bytes(b"fake")
        keyfile = tmp_path / "age.key"
        keyfile.write_bytes(b"k")
        keyfile.chmod(0o600)

        with _mock_age_success(_valid_json()):
            creds = load_credentials(credentials_path=creds_file, keyfile=keyfile)

        r = repr(creds)
        assert "s3cr3t" not in r
        assert "[REDACTED]" in r
