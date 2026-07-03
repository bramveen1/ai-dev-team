"""Unit tests for router.memory_retriever — index-backed retrieval (#640)."""

import pytest

from router.memory_index import build_index
from router.memory_retriever import (
    RETRIEVAL_FLAG_ENV,
    _tokenize,
    is_retrieval_enabled,
    retrieve_relevant_memory,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_base(tmp_path):
    """An agent tree with an indexed structured memory."""
    memory = tmp_path / "lisa" / "memory"
    (memory / "people").mkdir(parents=True)
    (memory / "people" / "bram.md").write_text("## 2026-07-01\nPrefers short PRs and ruff formatting.\n")
    (memory / "people" / "jane.md").write_text("## 2026-06-01\nDesigner on the landing page.\n")
    (memory / "projects").mkdir()
    (memory / "projects" / "ai-dev-team.md").write_text("## 2026-07-02\nRouter dispatches agents via Slack.\n")
    (memory / "decisions").mkdir()
    (memory / "decisions" / "2026-07-01.md").write_text("## Index format\nChose manifest.json over embeddings.\n")
    build_index(memory)
    return tmp_path


class TestFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(RETRIEVAL_FLAG_ENV, raising=False)
        assert is_retrieval_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_enabled_values(self, monkeypatch, value):
        monkeypatch.setenv(RETRIEVAL_FLAG_ENV, value)
        assert is_retrieval_enabled() is True

    def test_disabled_values(self, monkeypatch):
        monkeypatch.setenv(RETRIEVAL_FLAG_ENV, "0")
        assert is_retrieval_enabled() is False


class TestTokenize:
    def test_compound_tokens_kept_whole_and_split(self):
        tokens = _tokenize("working on ai-dev-team today")
        assert "ai-dev-team" in tokens
        assert "dev" in tokens
        assert "team" in tokens

    def test_stopwords_dropped(self):
        assert "the" not in _tokenize("the router")
        assert "router" in _tokenize("the router")


class TestRetrieve:
    def test_matches_entity_by_key(self, agent_base):
        results = retrieve_relevant_memory("lisa", "what does bram think?", agent_base=agent_base)
        paths = [p for p, _ in results]
        assert "people/bram.md" in paths

    def test_returns_content(self, agent_base):
        results = retrieve_relevant_memory("lisa", "bram", agent_base=agent_base)
        content = dict(results)["people/bram.md"]
        assert "short PRs" in content

    def test_key_match_outranks_summary_match(self, agent_base):
        # "ruff" appears only in bram's summary; "jane" is a key hit.
        results = retrieve_relevant_memory("lisa", "ask jane about ruff", agent_base=agent_base)
        paths = [p for p, _ in results]
        assert paths.index("people/jane.md") < paths.index("people/bram.md")

    def test_no_match_returns_empty(self, agent_base):
        assert retrieve_relevant_memory("lisa", "quantum blockchain webinar", agent_base=agent_base) == []

    def test_missing_manifest_falls_back_to_empty(self, tmp_path):
        (tmp_path / "lisa" / "memory").mkdir(parents=True)
        assert retrieve_relevant_memory("lisa", "bram", agent_base=tmp_path) == []

    def test_empty_query_returns_empty(self, agent_base):
        assert retrieve_relevant_memory("lisa", "", agent_base=agent_base) == []

    def test_top_k_limits_results(self, agent_base):
        results = retrieve_relevant_memory("lisa", "bram jane ai-dev-team manifest", agent_base=agent_base, top_k=1)
        assert len(results) == 1

    def test_byte_budget_limits_results(self, agent_base):
        results = retrieve_relevant_memory(
            "lisa", "bram jane ai-dev-team manifest", agent_base=agent_base, max_total_bytes=10
        )
        # The first match is always included; the budget stops further files.
        assert len(results) == 1

    def test_alias_match(self, agent_base, tmp_path):
        from router.memory_identity import AliasMap

        memory = agent_base / "lisa" / "memory"
        build_index(memory, AliasMap({"people": {"bram": ["Veenhof"]}}))
        results = retrieve_relevant_memory("lisa", "message from Veenhof", agent_base=agent_base)
        assert "people/bram.md" in [p for p, _ in results]

    def test_manifest_path_escaping_memory_dir_is_skipped(self, agent_base):
        import json

        memory = agent_base / "lisa" / "memory"
        manifest_path = memory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"].append(
            {
                "path": "../../secret.md",
                "category": "people",
                "key": "bram",
                "aliases": [],
                "summary": "bram bram bram",
                "mtime": 9999999999,
                "size": 10,
            }
        )
        manifest_path.write_text(json.dumps(manifest))
        (agent_base / "secret.md").write_text("do not read")
        results = retrieve_relevant_memory("lisa", "bram", agent_base=agent_base)
        assert all("secret" not in content for _, content in results)
