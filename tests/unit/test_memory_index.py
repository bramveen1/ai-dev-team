"""Unit tests for router.memory_index — manifest generation and smoke probe (#640)."""

import json

import pytest

from router.memory_identity import AliasMap
from router.memory_index import (
    INDEX_FILENAME,
    MANIFEST_FILENAME,
    _summarize,
    build_index,
    load_manifest,
    verify_index,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def memory_dir(tmp_path):
    """A small structured memory tree."""
    (tmp_path / "people").mkdir()
    (tmp_path / "people" / "bram.md").write_text("## 2026-07-01\nPrefers short PRs and ruff.\n")
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "ai-dev-team.md").write_text("## 2026-07-02\nRouter dispatches agents via Slack.\n")
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "2026-07-01.md").write_text("## Chose manifest index\nNo vector DB in v1.\n")
    (tmp_path / "memory.md").write_text("# Working set\n")
    return tmp_path


class TestSummarize:
    def test_skips_headings_and_blank_lines(self):
        assert _summarize("## 2026-07-01\n\nPrefers short PRs.\n") == "Prefers short PRs."

    def test_strips_list_markers(self):
        assert _summarize("- **2026-07-01** — likes tests first\n") == "2026-07-01** — likes tests first"

    def test_truncates_long_lines(self):
        summary = _summarize("x" * 500)
        assert len(summary) <= 140

    def test_empty_file(self):
        assert _summarize("") == ""


class TestBuildIndex:
    def test_writes_manifest_and_index_md(self, memory_dir):
        manifest = build_index(memory_dir)
        assert (memory_dir / MANIFEST_FILENAME).exists()
        assert (memory_dir / INDEX_FILENAME).exists()
        paths = {entry["path"] for entry in manifest["files"]}
        assert paths == {"people/bram.md", "projects/ai-dev-team.md", "decisions/2026-07-01.md"}

    def test_memory_md_is_not_indexed(self, memory_dir):
        manifest = build_index(memory_dir)
        assert all("memory.md" != entry["path"] for entry in manifest["files"])

    def test_entry_fields(self, memory_dir):
        manifest = build_index(memory_dir)
        entry = next(e for e in manifest["files"] if e["key"] == "bram")
        assert entry["category"] == "people"
        assert entry["summary"] == "Prefers short PRs and ruff."
        assert entry["size"] > 0
        assert entry["mtime"] > 0

    def test_aliases_attached_from_alias_map(self, memory_dir):
        alias_map = AliasMap({"people": {"bram": ["Bram Veenhof"]}})
        manifest = build_index(memory_dir, alias_map)
        entry = next(e for e in manifest["files"] if e["key"] == "bram")
        assert entry["aliases"] == ["Bram Veenhof"]

    def test_manifest_is_valid_json_on_disk(self, memory_dir):
        build_index(memory_dir)
        data = json.loads((memory_dir / MANIFEST_FILENAME).read_text())
        assert data["version"] == 1

    def test_index_md_lists_entries(self, memory_dir):
        build_index(memory_dir, AliasMap({"people": {"bram": ["Bram Veenhof"]}}))
        index_md = (memory_dir / INDEX_FILENAME).read_text()
        assert "**bram**" in index_md
        assert "aka Bram Veenhof" in index_md

    def test_empty_memory_dir(self, tmp_path):
        manifest = build_index(tmp_path)
        assert manifest["files"] == []


class TestLoadManifest:
    def test_roundtrip(self, memory_dir):
        build_index(memory_dir)
        manifest = load_manifest(memory_dir)
        assert manifest is not None
        assert len(manifest["files"]) == 3

    def test_missing_manifest_returns_none(self, tmp_path):
        assert load_manifest(tmp_path) is None

    def test_corrupt_manifest_returns_none(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text("{broken")
        assert load_manifest(tmp_path) is None

    def test_wrong_shape_returns_none(self, tmp_path):
        (tmp_path / MANIFEST_FILENAME).write_text('{"files": "nope"}')
        assert load_manifest(tmp_path) is None


class TestVerifyIndex:
    def test_healthy_index_has_no_problems(self, memory_dir):
        build_index(memory_dir)
        assert verify_index(memory_dir) == []

    def test_missing_manifest_is_a_problem(self, tmp_path):
        problems = verify_index(tmp_path)
        assert len(problems) == 1
        assert "missing" in problems[0]

    def test_detects_dangling_file_reference(self, memory_dir):
        build_index(memory_dir)
        (memory_dir / "people" / "bram.md").unlink()
        problems = verify_index(memory_dir)
        assert any("missing file" in p for p in problems)

    def test_detects_duplicate_canonical_resolution(self, memory_dir):
        """A known entity must resolve to exactly one file."""
        (memory_dir / "people" / "bram-veenhof.md").write_text("dup\n")
        alias_map = AliasMap({"people": {"bram": ["bram-veenhof"]}})
        build_index(memory_dir, alias_map)
        problems = verify_index(memory_dir, alias_map)
        assert any("resolves to 2 files" in p for p in problems)


class TestFtsDatabase:
    """FTS5 search.db generation alongside the manifest (#640)."""

    def test_build_index_creates_search_db(self, memory_dir):
        build_index(memory_dir)
        assert (memory_dir / "search.db").exists()

    def test_row_per_manifest_entry(self, memory_dir):
        import sqlite3

        manifest = build_index(memory_dir)
        conn = sqlite3.connect(memory_dir / "search.db")
        try:
            (count,) = conn.execute("SELECT count(*) FROM memory_fts").fetchone()
        finally:
            conn.close()
        assert count == len(manifest["files"])

    def test_verify_index_flags_missing_search_db(self, memory_dir):
        build_index(memory_dir)
        (memory_dir / "search.db").unlink()
        problems = verify_index(memory_dir)
        assert any("search.db is missing" in p for p in problems)

    def test_verify_index_flags_row_count_mismatch(self, memory_dir):
        build_index(memory_dir)
        (memory_dir / "people" / "new.md").write_text("added after index build\n")
        import json

        manifest_path = memory_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"].append(
            {
                "path": "people/new.md",
                "category": "people",
                "key": "new",
                "aliases": [],
                "summary": "",
                "mtime": 1,
                "size": 1,
            }
        )
        manifest_path.write_text(json.dumps(manifest))
        problems = verify_index(memory_dir)
        assert any("rows" in p for p in problems)
