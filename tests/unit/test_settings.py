"""Unit tests for router/settings.py — registry, precedence, hot reload, failure modes (#576)."""

from __future__ import annotations

import json

import pytest

from router import settings as settings_mod
from router.packs.secret_store import SecretStore
from router.settings import (
    REGISTRY,
    RuntimeSettings,
    Setting,
    validate_for_write,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    """A RuntimeSettings with ttl=0 (no caching between calls) on tmp paths."""
    return RuntimeSettings(
        path=tmp_path / "runtime.json",
        ttl=0.0,
        secret_store=SecretStore(path=tmp_path / "secrets.json"),
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every registry key from the environment so tests are hermetic."""
    for key in REGISTRY:
        monkeypatch.delenv(key, raising=False)


class TestRegistry:
    def test_all_entries_have_valid_shape(self):
        for key, entry in REGISTRY.items():
            assert entry.key == key
            assert entry.kind in ("var", "secret", "boot")
            assert entry.type in ("str", "int", "bool", "channel")
            assert entry.reload in ("hot", "restart")
            assert entry.description
            assert entry.category

    def test_secret_entries_have_secret_store_keys(self):
        for entry in REGISTRY.values():
            if entry.kind == "secret":
                assert entry.secret_key, f"{entry.key} missing secret_key"
                assert entry.is_sensitive

    def test_defaults_match_declared_types(self):
        for entry in REGISTRY.values():
            if entry.type == "int":
                assert isinstance(entry.default, int)
            elif entry.type == "bool":
                assert isinstance(entry.default, bool)
            else:
                assert isinstance(entry.default, str)

    def test_legacy_defaults_preserved(self):
        """Registry defaults must mirror the constants they replaced."""
        assert REGISTRY["SESSION_TIMEOUT"].default == 1800
        assert REGISTRY["MAX_CONTEXT_TOKENS"].default == 32000
        assert REGISTRY["STUCK_GUARD_TURN_CAP"].default == 50
        assert REGISTRY["STUCK_GUARD_MODE"].default == "dry-run"
        assert REGISTRY["LOG_LEVEL"].default == "INFO"


class TestPrecedence:
    def test_default_when_nothing_set(self, store):
        assert store.get("SESSION_TIMEOUT") == 1800
        assert store.source("SESSION_TIMEOUT") == "default"

    def test_env_fallback_when_file_absent(self, store, monkeypatch):
        monkeypatch.setenv("AUTO_DISPATCH_CHANNEL", "C123ABC456")
        assert store.get("AUTO_DISPATCH_CHANNEL") == "C123ABC456"
        assert store.source("AUTO_DISPATCH_CHANNEL") == "env"

    def test_store_wins_over_env(self, store, monkeypatch):
        monkeypatch.setenv("AUTO_DISPATCH_CHANNEL", "CENVENVENV")
        store.set("AUTO_DISPATCH_CHANNEL", "CFILEFILE1")
        assert store.get("AUTO_DISPATCH_CHANNEL") == "CFILEFILE1"
        assert store.source("AUTO_DISPATCH_CHANNEL") == "runtime"

    def test_empty_env_treated_as_unset(self, store, monkeypatch):
        """Compose renders ${VAR:-} as '' — must not shadow the default."""
        monkeypatch.setenv("SESSION_TIMEOUT", "")
        assert store.get("SESSION_TIMEOUT") == 1800
        assert store.source("SESSION_TIMEOUT") == "default"

    def test_explicit_empty_file_value_wins_over_env(self, store, monkeypatch):
        """An operator clearing a value in the UI must override a stale .env entry."""
        monkeypatch.setenv("AUTO_DISPATCH_CHANNEL", "CENVENVENV")
        store.set("AUTO_DISPATCH_CHANNEL", "")
        assert store.get("AUTO_DISPATCH_CHANNEL") == ""

    def test_unset_restores_env_fallback(self, store, monkeypatch):
        monkeypatch.setenv("AUTO_DISPATCH_CHANNEL", "CENVENVENV")
        store.set("AUTO_DISPATCH_CHANNEL", "CFILEFILE1")
        assert store.unset("AUTO_DISPATCH_CHANNEL") is True
        assert store.get("AUTO_DISPATCH_CHANNEL") == "CENVENVENV"
        assert store.unset("AUTO_DISPATCH_CHANNEL") is False

    def test_boot_kind_reads_env_only(self, store, monkeypatch):
        monkeypatch.setenv("CLAUDE_AUTH_MODE", "api_key")
        assert store.get("CLAUDE_AUTH_MODE") == "api_key"
        with pytest.raises(ValueError, match="boot environment"):
            store.set("CLAUDE_AUTH_MODE", "credentials")
        with pytest.raises(ValueError, match="boot environment"):
            store.unset("CLAUDE_AUTH_MODE")


class TestTypes:
    def test_bool_env_parsing(self, store, monkeypatch):
        for raw, expected in (("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("off", False)):
            monkeypatch.setenv("ATTACHMENTS_ENABLED", raw)
            assert store.get("ATTACHMENTS_ENABLED") is expected

    def test_bool_stored_as_json_boolean(self, store):
        store.set("ATTACHMENTS_ENABLED", "true")
        data = json.loads(store.path.read_text())
        assert data["ATTACHMENTS_ENABLED"] is True
        assert store.get("ATTACHMENTS_ENABLED") is True

    def test_int_coercion_from_strings(self, store):
        store.set("SESSION_TIMEOUT", "900")
        assert store.get("SESSION_TIMEOUT") == 900

    def test_int_rejects_garbage_on_write(self, store):
        with pytest.raises(ValueError, match="expected an integer"):
            store.set("SESSION_TIMEOUT", "soon")

    def test_int_min_value_enforced_on_write(self, store):
        with pytest.raises(ValueError, match=">= 1"):
            store.set("SESSION_TIMEOUT", 0)

    def test_channel_format_enforced_on_write(self, store):
        with pytest.raises(ValueError, match="does not look like a Slack conversation ID"):
            store.set("AUTO_DISPATCH_CHANNEL", "general")
        store.set("AUTO_DISPATCH_CHANNEL", "C0AGZC613NK")  # the #576 typo class, valid format
        assert store.get("AUTO_DISPATCH_CHANNEL") == "C0AGZC613NK"

    def test_choices_enforced_on_write(self, store):
        with pytest.raises(ValueError, match="must be one of"):
            store.set("STUCK_GUARD_MODE", "yolo")
        store.set("STUCK_GUARD_MODE", "enforce")
        assert store.get("STUCK_GUARD_MODE") == "enforce"

    def test_unknown_key_raises(self, store):
        with pytest.raises(KeyError):
            store.get("NOT_A_SETTING")
        with pytest.raises(KeyError):
            store.set("NOT_A_SETTING", "x")


class TestFailureModes:
    def test_malformed_file_keeps_last_good(self, store, caplog):
        store.set("SESSION_TIMEOUT", 900)
        assert store.get("SESSION_TIMEOUT") == 900
        store.path.write_text("{ not json", encoding="utf-8")
        with caplog.at_level("ERROR"):
            assert store.get("SESSION_TIMEOUT") == 900
        assert any("keeping last-good" in r.message for r in caplog.records)

    def test_non_object_root_keeps_last_good(self, store):
        store.set("SESSION_TIMEOUT", 900)
        store.path.write_text("[1, 2, 3]", encoding="utf-8")
        assert store.get("SESSION_TIMEOUT") == 900

    def test_invalid_file_value_falls_back_per_key(self, store, monkeypatch, caplog):
        """A bad value for one key must not disturb the others (validate-on-reload, #576)."""
        store.path.write_text(json.dumps({"SESSION_TIMEOUT": "eleventy", "STUCK_GUARD_TURN_CAP": 10}))
        monkeypatch.setenv("SESSION_TIMEOUT", "600")
        with caplog.at_level("WARNING"):
            assert store.get("SESSION_TIMEOUT") == 600  # falls through to env
        assert store.get("STUCK_GUARD_TURN_CAP") == 10  # unaffected
        assert any("invalid runtime-config value" in r.message.lower() for r in caplog.records)

    def test_invalid_env_value_falls_back_to_default(self, store, monkeypatch):
        monkeypatch.setenv("SESSION_TIMEOUT", "not-a-number")
        assert store.get("SESSION_TIMEOUT") == 1800

    def test_missing_file_is_empty_config(self, store):
        assert not store.path.exists()
        assert store.get("ATTACHMENTS_ENABLED") is False

    def test_write_is_atomic_no_tmp_left_behind(self, store):
        store.set("SESSION_TIMEOUT", 900)
        assert not store.path.with_suffix(".json.tmp").exists()
        assert json.loads(store.path.read_text())["SESSION_TIMEOUT"] == 900


class TestTTLReload:
    def test_external_edit_visible_after_ttl(self, tmp_path):
        """Simulates an operator editing runtime.json while the router runs."""
        rs = RuntimeSettings(path=tmp_path / "runtime.json", ttl=1000.0, secret_store=SecretStore(tmp_path / "s.json"))
        rs.path.write_text(json.dumps({"SESSION_TIMEOUT": 900}))
        assert rs.get("SESSION_TIMEOUT") == 900
        rs.path.write_text(json.dumps({"SESSION_TIMEOUT": 1200}))
        assert rs.get("SESSION_TIMEOUT") == 900  # still cached inside TTL
        rs._cache_read_at = None  # force expiry without sleeping
        assert rs.get("SESSION_TIMEOUT") == 1200

    def test_own_writes_visible_immediately_despite_ttl(self, tmp_path):
        rs = RuntimeSettings(path=tmp_path / "runtime.json", ttl=1000.0, secret_store=SecretStore(tmp_path / "s.json"))
        assert rs.get("SESSION_TIMEOUT") == 1800
        rs.set("SESSION_TIMEOUT", 900)
        assert rs.get("SESSION_TIMEOUT") == 900


class TestSecrets:
    def test_secret_roundtrip_via_secret_store(self, store):
        store.set("WORKERS_BOT_TOKEN", "xoxb-new-token")
        assert store.get("WORKERS_BOT_TOKEN") == "xoxb-new-token"
        assert store.source("WORKERS_BOT_TOKEN") == "secret-store"
        # Persisted under the legacy secret-store key so existing readers agree.
        assert store._secret_store.get_str("workers_bot_token") == "xoxb-new-token"

    def test_secret_store_wins_over_env(self, store, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-from-env")
        store.set("WORKERS_BOT_TOKEN", "xoxb-from-store")
        assert store.get("WORKERS_BOT_TOKEN") == "xoxb-from-store"

    def test_secret_env_fallback(self, store, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-from-env")
        assert store.get("WORKERS_BOT_TOKEN") == "xoxb-from-env"
        assert store.source("WORKERS_BOT_TOKEN") == "env"

    def test_secret_unset(self, store):
        store.set("WORKERS_BOT_TOKEN", "xoxb-x")
        assert store.unset("WORKERS_BOT_TOKEN") is True
        assert store.get("WORKERS_BOT_TOKEN") == ""
        assert store.unset("WORKERS_BOT_TOKEN") is False

    def test_secrets_never_written_to_runtime_json(self, store):
        store.set("WORKERS_BOT_TOKEN", "xoxb-x")
        assert not store.path.exists() or "WORKERS_BOT_TOKEN" not in json.loads(store.path.read_text())


class TestSingleton:
    def test_reset_and_module_level_get(self, tmp_path, monkeypatch):
        rs = RuntimeSettings(path=tmp_path / "runtime.json", ttl=0.0, secret_store=SecretStore(tmp_path / "s.json"))
        settings_mod.reset_settings_for_tests(rs)
        try:
            rs.set("SESSION_TIMEOUT", 777)
            assert settings_mod.get("SESSION_TIMEOUT") == 777
            assert settings_mod.get_settings() is rs
        finally:
            settings_mod.reset_settings_for_tests(None)

    def test_default_singleton_constructs(self):
        settings_mod.reset_settings_for_tests(None)
        try:
            assert isinstance(settings_mod.get_settings(), RuntimeSettings)
        finally:
            settings_mod.reset_settings_for_tests(None)


class TestValidateForWrite:
    def test_empty_string_always_accepted(self):
        entry = REGISTRY["AUTO_DISPATCH_CHANNEL"]
        assert validate_for_write(entry, "") == ""

    def test_channel_regex_examples(self):
        entry = Setting("X", "var", "channel", "", "d", "hot", "c")
        assert validate_for_write(entry, "C0AGZC613NKK") == "C0AGZC613NKK"
        assert validate_for_write(entry, "D024BE91L34") == "D024BE91L34"
        for bad in ("c0agzc613nk", "XABCDEFGH", "C123", "C 123ABC456"):
            with pytest.raises(ValueError):
                validate_for_write(entry, bad)
