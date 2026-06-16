"""Unit tests for router.packs.github_app.

All external calls (subprocess age, httpx, time.time) are monkeypatched.
SecretStore is constructed with a tmp_path-based JSON file — no os.environ
patching for secrets per the project convention.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from router.packs.github_app import (
    _decrypt_pem,
    _exchange_jwt_for_token,
    _make_jwt,
    _read_role_config,
    is_enabled,
    mint_installation_token,
)
from router.packs.secret_store import SecretStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_with_github_app(tmp_path: Path, data: dict) -> SecretStore:
    store = SecretStore(path=tmp_path / "secrets.json")
    store.set("github_app", data)
    return store


def _dispatch_store(tmp_path: Path, *, app_id: str = "100", installation_id: str = "200") -> SecretStore:
    return _store_with_github_app(tmp_path, {"dispatch": {"app_id": app_id, "installation_id": installation_id}})


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


class TestIsEnabled:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("USE_GITHUB_APP", raising=False)
        assert is_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "TRUE", "yes", "YES"])
    def test_enabled_truthy_values(self, val: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_GITHUB_APP", val)
        assert is_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_disabled_falsy_values(self, val: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_GITHUB_APP", val)
        assert is_enabled() is False


# ---------------------------------------------------------------------------
# _read_role_config
# ---------------------------------------------------------------------------


class TestReadRoleConfig:
    def test_returns_app_id_and_installation_id_for_dispatch(self, tmp_path: Path) -> None:
        store = _dispatch_store(tmp_path, app_id="123", installation_id="456")
        app_id, inst_id = _read_role_config("dispatch", store)
        assert app_id == "123"
        assert inst_id == "456"

    def test_returns_app_id_and_installation_id_for_review(self, tmp_path: Path) -> None:
        store = _store_with_github_app(tmp_path, {"review": {"app_id": "789", "installation_id": "101"}})
        app_id, inst_id = _read_role_config("review", store)
        assert app_id == "789"
        assert inst_id == "101"

    def test_coerces_integer_ids_to_str(self, tmp_path: Path) -> None:
        store = _store_with_github_app(tmp_path, {"dispatch": {"app_id": 999, "installation_id": 888}})
        app_id, inst_id = _read_role_config("dispatch", store)
        assert app_id == "999"
        assert inst_id == "888"

    def test_role_not_in_config_raises(self, tmp_path: Path) -> None:
        store = _store_with_github_app(tmp_path, {})
        with pytest.raises(RuntimeError, match="not configured"):
            _read_role_config("dispatch", store)

    def test_no_github_app_key_raises(self, tmp_path: Path) -> None:
        store = SecretStore(path=tmp_path / "secrets.json")
        with pytest.raises(RuntimeError, match="not configured"):
            _read_role_config("dispatch", store)

    def test_missing_app_id_raises(self, tmp_path: Path) -> None:
        store = _store_with_github_app(tmp_path, {"dispatch": {"installation_id": "456"}})
        with pytest.raises(RuntimeError, match="app_id missing"):
            _read_role_config("dispatch", store)

    def test_missing_installation_id_raises(self, tmp_path: Path) -> None:
        store = _store_with_github_app(tmp_path, {"dispatch": {"app_id": "123"}})
        with pytest.raises(RuntimeError, match="installation_id missing"):
            _read_role_config("dispatch", store)


# ---------------------------------------------------------------------------
# _decrypt_pem
# ---------------------------------------------------------------------------


class TestDecryptPem:
    def test_returns_stdout_on_success(self, tmp_path: Path) -> None:
        pem_path = tmp_path / "key.pem.age"
        pem_path.write_bytes(b"encrypted")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"-----BEGIN RSA PRIVATE KEY-----", stderr=b"")
            result = _decrypt_pem(pem_path)

        assert result == b"-----BEGIN RSA PRIVATE KEY-----"
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "age"
        assert "--decrypt" in call_args
        assert str(pem_path) in call_args

    def test_non_zero_exit_raises(self, tmp_path: Path) -> None:
        pem_path = tmp_path / "key.pem.age"
        pem_path.write_bytes(b"bad")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"bad passphrase")
            with pytest.raises(RuntimeError, match="age --decrypt failed"):
                _decrypt_pem(pem_path)

    def test_age_not_found_raises(self, tmp_path: Path) -> None:
        pem_path = tmp_path / "key.pem.age"
        pem_path.write_bytes(b"encrypted")

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="age CLI not found"):
                _decrypt_pem(pem_path)

    def test_timeout_raises(self, tmp_path: Path) -> None:
        pem_path = tmp_path / "key.pem.age"
        pem_path.write_bytes(b"encrypted")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("age", 10)):
            with pytest.raises(RuntimeError, match="timed out"):
                _decrypt_pem(pem_path)

    def test_stderr_included_in_error_message(self, tmp_path: Path) -> None:
        pem_path = tmp_path / "key.pem.age"
        pem_path.write_bytes(b"encrypted")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=2, stdout=b"", stderr=b"no identity found")
            with pytest.raises(RuntimeError, match="no identity found"):
                _decrypt_pem(pem_path)


# ---------------------------------------------------------------------------
# _make_jwt
# ---------------------------------------------------------------------------


class TestMakeJwt:
    @pytest.fixture()
    def rsa_pem(self) -> bytes:
        """Generate a fresh RSA-2048 private key in PEM format."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def test_produces_valid_rs256_jwt(self, rsa_pem: bytes) -> None:
        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_key = key.public_key()

        with patch("time.time", return_value=1_000_000):
            token = _make_jwt("app-42", pem)

        decoded = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        assert decoded["iss"] == "app-42"
        assert decoded["iat"] == 1_000_000 - 60
        assert decoded["exp"] == 1_000_000 + 600

    def test_iat_is_backdated_60s(self, rsa_pem: bytes) -> None:
        import jwt as pyjwt

        with patch("time.time", return_value=2_000_000):
            token = _make_jwt("app-99", rsa_pem)

        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == "RS256"
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["iat"] == 2_000_000 - 60

    def test_exp_is_10_minutes(self, rsa_pem: bytes) -> None:
        import jwt as pyjwt

        with patch("time.time", return_value=3_000_000):
            token = _make_jwt("app-1", rsa_pem)

        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["exp"] == 3_000_000 + 600


# ---------------------------------------------------------------------------
# _exchange_jwt_for_token
# ---------------------------------------------------------------------------


class TestExchangeJwtForToken:
    def _mock_client(self, status: int, body: dict) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = body
        mock_resp.text = str(body)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        return mock_client

    def test_returns_token_on_201(self) -> None:
        mock_client = self._mock_client(201, {"token": "ghs_abc123", "expires_at": "2026-06-16T10:00:00Z"})

        with patch("httpx.Client", return_value=mock_client):
            token = _exchange_jwt_for_token("12345", "jwt.header.sig")

        assert token == "ghs_abc123"

    def test_posts_to_correct_url(self) -> None:
        mock_client = self._mock_client(201, {"token": "ghs_x"})

        with patch("httpx.Client", return_value=mock_client):
            _exchange_jwt_for_token("99999", "jwt.tok")

        call_url = mock_client.post.call_args[0][0]
        assert "99999" in call_url
        assert "access_tokens" in call_url

    def test_authorization_header_uses_bearer(self) -> None:
        mock_client = self._mock_client(201, {"token": "ghs_y"})

        with patch("httpx.Client", return_value=mock_client):
            _exchange_jwt_for_token("111", "my.jwt.token")

        headers = mock_client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer my.jwt.token"

    def test_non_201_status_raises(self) -> None:
        mock_client = self._mock_client(401, {"message": "Bad credentials"})

        with patch("httpx.Client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="401"):
                _exchange_jwt_for_token("12345", "jwt.tok")

    def test_404_raises(self) -> None:
        mock_client = self._mock_client(404, {"message": "Not Found"})

        with patch("httpx.Client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="404"):
                _exchange_jwt_for_token("99999", "jwt.tok")

    def test_missing_token_in_body_raises(self) -> None:
        mock_client = self._mock_client(201, {})

        with patch("httpx.Client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="no token"):
                _exchange_jwt_for_token("12345", "jwt.tok")


# ---------------------------------------------------------------------------
# mint_installation_token (integration of all internal helpers)
# ---------------------------------------------------------------------------


class TestMintInstallationToken:
    def test_happy_path_dispatch(self, tmp_path: Path) -> None:
        store = _dispatch_store(tmp_path)
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        (keys_dir / "aidt-dispatch.pem.age").write_bytes(b"encrypted")

        with (
            patch("router.packs.github_app._decrypt_pem", return_value=b"PEM") as mock_decrypt,
            patch("router.packs.github_app._make_jwt", return_value="jwt-tok") as mock_jwt,
            patch("router.packs.github_app._exchange_jwt_for_token", return_value="ghs_abc") as mock_exchange,
        ):
            result = mint_installation_token("dispatch", secret_store=store, keys_dir=keys_dir)

        assert result == "ghs_abc"
        mock_decrypt.assert_called_once()
        mock_jwt.assert_called_once_with("100", b"PEM")
        mock_exchange.assert_called_once_with("200", "jwt-tok")

    def test_happy_path_review(self, tmp_path: Path) -> None:
        store = _store_with_github_app(tmp_path, {"review": {"app_id": "300", "installation_id": "400"}})
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        (keys_dir / "aidt-review.pem.age").write_bytes(b"encrypted")

        with (
            patch("router.packs.github_app._decrypt_pem", return_value=b"PEM"),
            patch("router.packs.github_app._make_jwt", return_value="jwt-tok"),
            patch("router.packs.github_app._exchange_jwt_for_token", return_value="ghs_review") as mock_exchange,
        ):
            result = mint_installation_token("review", secret_store=store, keys_dir=keys_dir)

        assert result == "ghs_review"
        mock_exchange.assert_called_once_with("400", "jwt-tok")

    def test_missing_pem_file_raises(self, tmp_path: Path) -> None:
        store = _dispatch_store(tmp_path)
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        # PEM file intentionally not created

        with pytest.raises(RuntimeError, match="PEM key file not found"):
            mint_installation_token("dispatch", secret_store=store, keys_dir=keys_dir)

    def test_missing_config_raises_without_calling_decrypt(self, tmp_path: Path) -> None:
        store = SecretStore(path=tmp_path / "secrets.json")
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()

        with patch("router.packs.github_app._decrypt_pem") as mock_decrypt:
            with pytest.raises(RuntimeError, match="not configured"):
                mint_installation_token("dispatch", secret_store=store, keys_dir=keys_dir)
        mock_decrypt.assert_not_called()

    def test_decrypt_failure_propagates(self, tmp_path: Path) -> None:
        store = _dispatch_store(tmp_path)
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        (keys_dir / "aidt-dispatch.pem.age").write_bytes(b"encrypted")

        with patch(
            "router.packs.github_app._decrypt_pem", side_effect=RuntimeError("age --decrypt failed (exit 1): oops")
        ):
            with pytest.raises(RuntimeError, match="age --decrypt failed"):
                mint_installation_token("dispatch", secret_store=store, keys_dir=keys_dir)

    def test_exchange_failure_propagates(self, tmp_path: Path) -> None:
        store = _dispatch_store(tmp_path)
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        (keys_dir / "aidt-dispatch.pem.age").write_bytes(b"encrypted")

        with (
            patch("router.packs.github_app._decrypt_pem", return_value=b"PEM"),
            patch("router.packs.github_app._make_jwt", return_value="jwt-tok"),
            patch("router.packs.github_app._exchange_jwt_for_token", side_effect=RuntimeError("GitHub 401")),
        ):
            with pytest.raises(RuntimeError, match="GitHub 401"):
                mint_installation_token("dispatch", secret_store=store, keys_dir=keys_dir)

    def test_uses_default_secret_store_when_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """mint_installation_token constructs a SecretStore when none supplied."""
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        (keys_dir / "aidt-dispatch.pem.age").write_bytes(b"encrypted")

        fake_store = MagicMock()
        fake_store.get.return_value = {"dispatch": {"app_id": "1", "installation_id": "2"}}

        with (
            patch("router.packs.github_app.SecretStore", return_value=fake_store),
            patch("router.packs.github_app._decrypt_pem", return_value=b"PEM"),
            patch("router.packs.github_app._make_jwt", return_value="jwt"),
            patch("router.packs.github_app._exchange_jwt_for_token", return_value="ghs_tok"),
        ):
            result = mint_installation_token("dispatch", keys_dir=keys_dir)

        assert result == "ghs_tok"
