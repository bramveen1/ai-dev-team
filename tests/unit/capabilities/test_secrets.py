"""Tests for the generic file-based secrets store."""

from __future__ import annotations

import json
import time

import pytest

from capabilities.secrets import SecretsError, SecretStore

pytestmark = pytest.mark.unit


@pytest.fixture
def secrets_dir(tmp_path):
    """Create a temporary secrets directory."""
    d = tmp_path / "secrets"
    d.mkdir()
    return d


@pytest.fixture
def store(secrets_dir):
    """Create a SecretStore backed by the temp directory."""
    return SecretStore(secrets_dir)


def _write_secrets(secrets_dir, provider, data):
    """Write a secrets file to the temp directory."""
    path = secrets_dir / f"{provider}.json"
    path.write_text(json.dumps(data))


class TestLoad:
    def test_load_valid_file(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"client_id": "abc", "access_token": "tok123"})
        data = store.load("m365")
        assert data["client_id"] == "abc"
        assert data["access_token"] == "tok123"

    def test_load_missing_file_raises(self, store):
        with pytest.raises(SecretsError, match="not found"):
            store.load("nonexistent")

    def test_load_invalid_json_raises(self, store, secrets_dir):
        (secrets_dir / "bad.json").write_text("not json{{{")
        with pytest.raises(SecretsError, match="Invalid JSON"):
            store.load("bad")

    def test_load_non_object_raises(self, store, secrets_dir):
        (secrets_dir / "arr.json").write_text('["a", "b"]')
        with pytest.raises(SecretsError, match="JSON object"):
            store.load("arr")

    def test_load_caches_result(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"key": "val"})
        data1 = store.load("m365")
        data2 = store.load("m365")
        assert data1 is data2


class TestSave:
    def test_save_creates_file(self, store, secrets_dir):
        store.save("github", {"token": "ghp_abc"})
        path = secrets_dir / "github.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["token"] == "ghp_abc"

    def test_save_creates_directory(self, tmp_path):
        new_dir = tmp_path / "new_secrets"
        s = SecretStore(new_dir)
        s.save("test", {"key": "val"})
        assert (new_dir / "test.json").exists()

    def test_save_invalidates_cache(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"key": "old"})
        store.load("m365")
        store.save("m365", {"key": "new"})
        data = store.load("m365")
        assert data["key"] == "new"


class TestGet:
    def test_get_existing_key(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"access_token": "tok"})
        assert store.get("m365", "access_token") == "tok"

    def test_get_missing_key_returns_none(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"access_token": "tok"})
        assert store.get("m365", "nonexistent") is None

    def test_get_missing_provider_returns_none(self, store):
        assert store.get("nonexistent", "key") is None

    def test_get_converts_to_string(self, store, secrets_dir):
        _write_secrets(secrets_dir, "test", {"number": 42, "bool": True})
        assert store.get("test", "number") == "42"
        assert store.get("test", "bool") == "True"


class TestSet:
    def test_set_creates_new_provider(self, store, secrets_dir):
        store.set("new_provider", "api_key", "secret123")
        data = json.loads((secrets_dir / "new_provider.json").read_text())
        assert data["api_key"] == "secret123"

    def test_set_updates_existing_key(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"access_token": "old"})
        store.set("m365", "access_token", "new")
        assert store.get("m365", "access_token") == "new"

    def test_set_preserves_other_keys(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"client_id": "abc", "access_token": "old"})
        store.set("m365", "access_token", "new")
        data = store.load("m365")
        assert data["client_id"] == "abc"
        assert data["access_token"] == "new"


class TestNeedsRefresh:
    def test_expired_token(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"expires_at": int(time.time()) - 100})
        assert store.needs_refresh("m365") is True

    def test_expiring_soon(self, store, secrets_dir):
        # Expires in 2 minutes, but buffer is 5 minutes
        _write_secrets(secrets_dir, "m365", {"expires_at": int(time.time()) + 120})
        assert store.needs_refresh("m365") is True

    def test_fresh_token(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"expires_at": int(time.time()) + 3600})
        assert store.needs_refresh("m365") is False

    def test_no_expires_at(self, store, secrets_dir):
        _write_secrets(secrets_dir, "zoho", {"api_key": "key"})
        assert store.needs_refresh("zoho") is False

    def test_missing_provider(self, store):
        assert store.needs_refresh("nonexistent") is False

    def test_invalid_expires_at(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"expires_at": "not-a-number"})
        assert store.needs_refresh("m365") is True


class TestInvalidate:
    def test_invalidate_provider(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"key": "val1"})
        store.load("m365")
        _write_secrets(secrets_dir, "m365", {"key": "val2"})
        store.invalidate("m365")
        data = store.load("m365")
        assert data["key"] == "val2"

    def test_invalidate_all(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"key": "val"})
        _write_secrets(secrets_dir, "zoho", {"key": "val"})
        store.load("m365")
        store.load("zoho")
        store.invalidate()
        assert store._cache == {}


class TestResolveEnvValue:
    def test_resolve_from_secrets_map(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"access_token": "real_token"})
        secrets_map = {"M365_ACCESS_TOKEN": "m365:access_token"}
        result = store.resolve_env_value("M365_ACCESS_TOKEN", secrets_map)
        assert result == "real_token"

    def test_resolve_fallback_to_environ(self, store, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
        result = store.resolve_env_value("GITHUB_TOKEN", {})
        assert result == "ghp_fallback"

    def test_resolve_not_found_returns_none(self, store):
        result = store.resolve_env_value("UNKNOWN_VAR", {})
        assert result is None

    def test_resolve_secrets_map_missing_key(self, store, secrets_dir):
        _write_secrets(secrets_dir, "m365", {"client_id": "abc"})
        secrets_map = {"M365_ACCESS_TOKEN": "m365:access_token"}
        # access_token not in file, should fall back
        result = store.resolve_env_value("M365_ACCESS_TOKEN", secrets_map)
        assert result is None

    def test_resolve_prefers_secrets_over_environ(self, store, secrets_dir, monkeypatch):
        _write_secrets(secrets_dir, "m365", {"access_token": "from_store"})
        monkeypatch.setenv("M365_ACCESS_TOKEN", "from_env")
        secrets_map = {"M365_ACCESS_TOKEN": "m365:access_token"}
        result = store.resolve_env_value("M365_ACCESS_TOKEN", secrets_map)
        assert result == "from_store"
