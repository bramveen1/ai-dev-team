"""Integration tests for context assembly from real fixtures.

Tests that the context builder can assemble a full context from
test fixture files and verify the result meets token budget constraints.
Tests will SKIP until the required modules exist.
"""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.integration


class TestContextAssemblyFromFixtures:
    """Test context assembly using actual fixture files."""

    def test_assemble_from_fixture_files(self):
        """Should build context from real fixture files."""
        try:
            from router.context_builder import build_context
        except ImportError:
            pytest.skip("router.context_builder not yet implemented")

        role_md = (FIXTURES_DIR / "role_files" / "lisa_role.md").read_text()
        memory = (FIXTURES_DIR / "memory" / "agents" / "lisa" / "memory" / "memory.md").read_text()
        system_docs = (FIXTURES_DIR / "systems" / "outlook.md").read_text()
        thread_history = [
            {"user": "U0001", "text": "Hey Lisa, review the outlook integration", "ts": "1.0"},
        ]

        result = build_context(
            role_md=role_md,
            memory=memory,
            thread_history=thread_history,
            system_docs=system_docs,
        )

        assert "Lisa" in result
        assert "outlook" in result.lower()

    def test_assemble_with_soul_and_personality(self):
        """Should build context including WORLDVIEW and personality from fixture files."""
        try:
            from router.context_builder import build_context
        except ImportError:
            pytest.skip("router.context_builder not yet implemented")

        worldview_md = (FIXTURES_DIR / "memory" / "shared" / "WORLDVIEW.md").read_text()
        role_md = (FIXTURES_DIR / "role_files" / "lisa_role.md").read_text()
        personality_md = (FIXTURES_DIR / "memory" / "agents" / "lisa" / "personality.md").read_text()
        memory = (FIXTURES_DIR / "memory" / "agents" / "lisa" / "memory" / "memory.md").read_text()

        result = build_context(
            role_md=role_md,
            memory=memory,
            thread_history=[],
            system_docs="",
            worldview_md=worldview_md,
            personality_md=personality_md,
        )

        # WORLDVIEW content present
        assert "genuinely helpful" in result
        # Personality content present
        assert "warm" in result.lower()
        # Role content present
        assert "Lisa" in result
        # Correct order: WORLDVIEW before role, role before personality
        assert result.index("genuinely helpful") < result.index("Lisa")
        assert result.index("Lisa") < result.index("warm")

    def test_context_within_token_budget(self):
        """Assembled context should respect the token budget."""
        try:
            from router.context_builder import build_context, estimate_tokens, truncate_to_budget
        except ImportError:
            pytest.skip("router.context_builder not yet implemented")

        role_md = (FIXTURES_DIR / "role_files" / "lisa_role.md").read_text()
        memory = (FIXTURES_DIR / "memory" / "agents" / "lisa" / "memory" / "memory.md").read_text()

        result = build_context(
            role_md=role_md,
            memory=memory,
            thread_history=[],
            system_docs="",
        )

        budget = 4000
        truncated = truncate_to_budget(result, max_tokens=budget)
        assert estimate_tokens(truncated) <= budget

    def test_context_with_empty_memory(self):
        """Should handle missing/empty memory gracefully."""
        try:
            from router.context_builder import build_context
        except ImportError:
            pytest.skip("router.context_builder not yet implemented")

        role_md = (FIXTURES_DIR / "role_files" / "lisa_role.md").read_text()

        result = build_context(
            role_md=role_md,
            memory="",
            thread_history=[],
            system_docs="",
        )

        assert "Lisa" in result
