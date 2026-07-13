"""Unit tests for router.memory_identity — canonical alias resolution (#640)."""

import json

import pytest

from router import memory_identity
from router.memory_identity import AliasMap, load_alias_map, sanitize_name

pytestmark = pytest.mark.unit


RAW_MAP = {
    "people": {
        "bram": ["Bram Veenhof", "bramveen1", "bramveenhof@gmail.com", "U0AHCJEHVNJ"],
    },
    "projects": {
        "ai-dev-team": ["ai dev team", "aidevteam"],
    },
    "systems": {},
}


class TestSanitizeName:
    def test_lowercases_and_hyphenates(self):
        assert sanitize_name("Bram Veenhof") == "bram-veenhof"

    def test_strips_leading_dots(self):
        assert sanitize_name("..sneaky") == "sneaky"

    def test_empty_falls_back_to_unknown(self):
        assert sanitize_name("///") == "unknown"


class TestAliasMap:
    def test_alias_resolves_to_canonical(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("people", "Bram Veenhof") == "bram"
        assert alias_map.resolve("people", "bramveenhof@gmail.com") == "bram"
        assert alias_map.resolve("projects", "AI Dev Team") == "ai-dev-team"

    def test_canonical_resolves_to_itself(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("people", "Bram") == "bram"

    def test_unknown_name_keeps_own_slug(self):
        """Never merge on a guess — unknown entities get their own file."""
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("people", "Jane Doe") == "jane-doe"

    def test_unknown_category_falls_back_to_sanitize(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("nonsense", "Bram Veenhof") == "bram-veenhof"

    def test_aliases_for_returns_original_strings(self):
        alias_map = AliasMap(RAW_MAP)
        assert "Bram Veenhof" in alias_map.aliases_for("people", "bram")
        assert alias_map.aliases_for("people", "jane") == []

    def test_conflicting_alias_keeps_first_mapping(self):
        raw = {"people": {"alice": ["Al"], "albert": ["Al"]}}
        alias_map = AliasMap(raw)
        assert alias_map.resolve("people", "Al") == "alice"

    def test_empty_map_is_plain_sanitization(self):
        alias_map = AliasMap()
        assert alias_map.resolve("people", "Bram Veenhof") == "bram-veenhof"

    def test_canonical_keys(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.canonical_keys("people") == ["bram"]


class TestCompoundIdentityResolution:
    """Issue #715: writer must not re-fragment on compound identity strings."""

    def test_compound_known_fields_merge_to_canonical(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("people", "Bram Veenhof (bramveen1)") == "bram"

    def test_different_known_field_combos_land_on_same_slug(self):
        """Two writes for the same person, different known fields each time."""
        alias_map = AliasMap(RAW_MAP)
        first = alias_map.resolve("people", "Bram, U0AHCJEHVNJ")
        second = alias_map.resolve("people", "bram-veenhof--U0AHCJEHVNJ--bramveen1--bramveenhof@gmail.com")
        assert first == second == "bram"

    def test_already_fragmented_stem_resolves_same_as_migrator_sees_it(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("people", "bram--bramveen1") == "bram"

    def test_unknown_compound_identity_is_one_stable_slug_not_a_permutation(self):
        alias_map = AliasMap(RAW_MAP)
        first = alias_map.resolve("people", "Jane Doe (jdoe)")
        second = alias_map.resolve("people", "jdoe, Jane Doe")
        assert first == second
        assert first == "--".join(sorted(["jane-doe", "jdoe"]))

    def test_two_distinct_known_canonicals_are_not_merged(self):
        raw = {"people": {"bram": ["Bram Veenhof"], "alice": ["Alice Smith"]}}
        alias_map = AliasMap(raw)
        result = alias_map.resolve("people", "Bram Veenhof, Alice Smith")
        assert result not in ("bram", "alice")
        assert result == "--".join(sorted(["bram-veenhof", "alice-smith"]))

    def test_single_part_slug_unchanged_no_behaviour_change(self):
        alias_map = AliasMap(RAW_MAP)
        assert alias_map.resolve("people", "Jane-Doe") == "jane-doe"

    def test_verify_index_idempotency_on_already_canonical_output(self):
        alias_map = AliasMap(RAW_MAP)
        merged = alias_map.resolve("people", "bram--bramveen1")
        assert alias_map.resolve("people", merged) == merged == "bram"


class TestLoadAliasMap:
    def test_loads_from_file(self, tmp_path):
        path = tmp_path / "memory-aliases.json"
        path.write_text(json.dumps(RAW_MAP))
        alias_map = load_alias_map(path)
        assert alias_map.resolve("people", "bramveen1") == "bram"

    def test_missing_file_degrades_to_empty_map(self, tmp_path):
        alias_map = load_alias_map(tmp_path / "nope.json")
        assert alias_map.resolve("people", "Bram Veenhof") == "bram-veenhof"

    def test_invalid_json_degrades_to_empty_map(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        alias_map = load_alias_map(path)
        assert alias_map.resolve("people", "anyone") == "anyone"

    def test_non_object_json_degrades_to_empty_map(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2]")
        alias_map = load_alias_map(path)
        assert alias_map.resolve("projects", "X") == "x"

    def test_env_var_overrides_default_path(self, tmp_path, monkeypatch):
        path = tmp_path / "custom.json"
        path.write_text(json.dumps(RAW_MAP))
        monkeypatch.setenv(memory_identity.ALIAS_MAP_ENV_VAR, str(path))
        alias_map = load_alias_map()
        assert alias_map.resolve("people", "U0AHCJEHVNJ") == "bram"
