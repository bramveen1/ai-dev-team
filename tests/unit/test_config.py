"""Unit tests for router.config — agent map loading and env var parsing.

These tests define the interface that router/config.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

config = pytest.importorskip("router.config", reason="router.config not yet implemented")

pytestmark = pytest.mark.unit


class TestAgentMap:
    """Tests for the agent map configuration."""

    def test_agent_map_returns_dict(self):
        """get_agent_map() should return a dictionary."""
        agent_map = config.get_agent_map()
        assert isinstance(agent_map, dict)

    def test_agent_map_has_lisa(self):
        """Agent map should contain a 'lisa' entry."""
        agent_map = config.get_agent_map()
        assert "lisa" in agent_map

    def test_agent_entry_has_required_fields(self):
        """Each agent entry should have 'name', 'container', and 'role_file' keys."""
        agent_map = config.get_agent_map()
        for agent_name, agent_config in agent_map.items():
            assert "name" in agent_config, f"Agent '{agent_name}' missing 'name'"
            assert "container" in agent_config, f"Agent '{agent_name}' missing 'container'"
            assert "role_file" in agent_config, f"Agent '{agent_name}' missing 'role_file'"


class TestEnvVarParsing:
    """Tests for environment variable loading with defaults."""

    def test_slack_bot_token_from_env(self, monkeypatch):
        """Should read SLACK_BOT_TOKEN from environment."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-123")
        cfg = config.load_config()
        assert cfg["slack_bot_token"] == "xoxb-test-123"

    def test_session_timeout_default(self, monkeypatch):
        """Should use default session timeout when env var is not set."""
        monkeypatch.delenv("SESSION_TIMEOUT", raising=False)
        cfg = config.load_config()
        assert isinstance(cfg["session_timeout"], int)
        assert cfg["session_timeout"] > 0

    def test_session_timeout_from_env(self, monkeypatch):
        """Should parse SESSION_TIMEOUT from environment as integer."""
        monkeypatch.setenv("SESSION_TIMEOUT", "600")
        cfg = config.load_config()
        assert cfg["session_timeout"] == 600

    def test_max_token_budget_default(self, monkeypatch):
        """Should have a default max token budget."""
        monkeypatch.delenv("MAX_TOKEN_BUDGET", raising=False)
        cfg = config.load_config()
        assert isinstance(cfg["max_token_budget"], int)
        assert cfg["max_token_budget"] > 0
