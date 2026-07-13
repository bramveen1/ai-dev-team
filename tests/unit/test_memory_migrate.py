"""Unit tests for router.memory_migrate — one-time dedup/archive migration (#640)."""

import json

import pytest

from router.memory_identity import AliasMap
from router.memory_migrate import migrate_agent_memory

pytestmark = pytest.mark.unit


ALIAS_MAP = AliasMap(
    {
        "people": {"bram": ["bram-veenhof", "u0ahcjehvnj"]},
        "projects": {"ai-dev-team": ["ai-dev-team-router"]},
    }
)


@pytest.fixture
def fragmented_memory(tmp_path):
    """A memory dir exhibiting the fragmentation from issue #640."""
    memory = tmp_path / "memory"
    people = memory / "people"
    people.mkdir(parents=True)
    (people / "bram.md").write_text("## 2026-06-01\ncanonical entry\n")
    (people / "bram-veenhof.md").write_text("## 2026-06-02\nvariant entry\n")
    (people / "u0ahcjehvnj.md").write_text("## 2026-06-03\nslack-id entry\n")
    (people / "jane.md").write_text("## 2026-06-04\nunknown entity stays put\n")
    projects = memory / "projects"
    projects.mkdir()
    (projects / "ai-dev-team-router.md").write_text("## 2026-06-05\nrouter notes\n")
    (memory / "salvage-251").mkdir()
    (memory / "salvage-251" / "junk.md").write_text("old salvage\n")
    (memory / "memory.md.bak-20260617").write_text("old backup\n")
    (memory / "memory.md").write_text("# Working set\n")
    return memory


class TestDryRun:
    def test_dry_run_changes_nothing(self, fragmented_memory):
        actions = migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=False)
        assert actions  # work was planned...
        assert (fragmented_memory / "people" / "bram-veenhof.md").exists()  # ...but not done
        assert (fragmented_memory / "salvage-251").exists()
        assert not (fragmented_memory / "manifest.json").exists()
        assert not (fragmented_memory / "_archive").exists()


class TestApply:
    def test_variants_merge_into_canonical(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        people = fragmented_memory / "people"
        content = (people / "bram.md").read_text()
        assert "canonical entry" in content
        assert "variant entry" in content
        assert "slack-id entry" in content
        assert "merged from people/bram-veenhof.md" in content
        assert not (people / "bram-veenhof.md").exists()
        assert not (people / "u0ahcjehvnj.md").exists()

    def test_unknown_entity_untouched(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        assert (fragmented_memory / "people" / "jane.md").read_text() == "## 2026-06-04\nunknown entity stays put\n"

    def test_variant_without_canonical_file_creates_it(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        projects = fragmented_memory / "projects"
        assert (projects / "ai-dev-team.md").exists()
        assert "router notes" in (projects / "ai-dev-team.md").read_text()
        assert not (projects / "ai-dev-team-router.md").exists()

    def test_reversible_originals_archived_not_deleted(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        archive = fragmented_memory / "_archive"
        assert (archive / "people" / "bram-veenhof.md").read_text() == "## 2026-06-02\nvariant entry\n"
        assert (archive / "salvage-251" / "junk.md").exists()
        assert (archive / "memory.md.bak-20260617").exists()

    def test_cruft_removed_from_memory_root(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        assert not (fragmented_memory / "salvage-251").exists()
        assert not (fragmented_memory / "memory.md.bak-20260617").exists()

    def test_index_built_and_canonical_unique(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        manifest = json.loads((fragmented_memory / "manifest.json").read_text())
        people_keys = [e["key"] for e in manifest["files"] if e["category"] == "people"]
        assert sorted(people_keys) == ["bram", "jane"]

    def test_idempotent_second_run_is_a_noop(self, fragmented_memory):
        migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        actions = migrate_agent_memory(fragmented_memory, ALIAS_MAP, apply=True)
        assert [a for a in actions if a.startswith(("merge", "archive"))] == []
