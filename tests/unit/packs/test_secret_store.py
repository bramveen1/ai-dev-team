"""Unit tests for router.packs.secret_store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from router.packs.secret_store import SecretStore

pytestmark = pytest.mark.unit


@pytest.fixture()
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(path=tmp_path / "secrets.json")


class TestSecretStore:
    def test_get_missing_returns_empty(self, store: SecretStore) -> None:
        assert store.get("github") == {}

    def test_set_and_get(self, store: SecretStore) -> None:
        store.set("github", {"GITHUB_TOKEN": "ghp_xyz"})
        assert store.get("github") == {"GITHUB_TOKEN": "ghp_xyz"}

    def test_set_persists_to_disk(self, store: SecretStore) -> None:
        store.set("github", {"GITHUB_TOKEN": "ghp_xyz"})
        assert json.loads(store.path.read_text()) == {"github": {"GITHUB_TOKEN": "ghp_xyz"}}

    def test_set_overwrites(self, store: SecretStore) -> None:
        store.set("github", {"old": "value"})
        store.set("github", {"new": "value"})
        assert store.get("github") == {"new": "value"}

    def test_multiple_packs(self, store: SecretStore) -> None:
        store.set("github", {"GITHUB_TOKEN": "x"})
        store.set("posthog", {"POSTHOG_API_KEY": "y"})
        assert store.get("github") == {"GITHUB_TOKEN": "x"}
        assert store.get("posthog") == {"POSTHOG_API_KEY": "y"}

    def test_delete_existing(self, store: SecretStore) -> None:
        store.set("github", {"GITHUB_TOKEN": "x"})
        assert store.delete("github") is True
        assert store.get("github") == {}

    def test_delete_missing(self, store: SecretStore) -> None:
        assert store.delete("nope") is False

    def test_has(self, store: SecretStore) -> None:
        assert store.has("github") is False
        store.set("github", {"GITHUB_TOKEN": "x"})
        assert store.has("github") is True

    def test_get_returns_copy(self, store: SecretStore) -> None:
        """Mutating the returned dict must not corrupt the on-disk store."""
        store.set("github", {"GITHUB_TOKEN": "x"})
        block = store.get("github")
        block["mutated"] = "hacked"
        assert store.get("github") == {"GITHUB_TOKEN": "x"}

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "secrets.json"
        store = SecretStore(path=nested)
        store.set("github", {"GITHUB_TOKEN": "x"})
        assert nested.exists()

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.json"
        path.write_text("not json {{{")
        store = SecretStore(path=path)
        with pytest.raises(ValueError, match="not valid JSON"):
            store.get("github")

    def test_root_must_be_object(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.json"
        path.write_text('["not", "an", "object"]')
        store = SecretStore(path=path)
        with pytest.raises(ValueError, match="must be a JSON object"):
            store.get("github")
