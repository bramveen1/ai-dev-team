"""Unit tests for router.config — agent map loading and env var parsing.

These tests define the interface that router/config.py must implement.
Tests will SKIP until the module exists.
"""

import textwrap
from pathlib import Path

import pytest

config = pytest.importorskip("router.config", reason="router.config not yet implemented")

pytestmark = pytest.mark.unit


@pytest.fixture
def agents_dir(tmp_path):
    """Temp agents directory with a stub 'lisa' agent.yaml."""
    agent_dir = tmp_path / "lisa"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        textwrap.dedent("""\
            name: Lisa
            container: lisa
            thinking_status: ""
        """)
    )
    return tmp_path


class TestAgentMap:
    """Tests for the agent map configuration."""

    def test_agent_map_returns_dict(self, agents_dir):
        """discover_agents() should return a dictionary."""
        agent_map = config.discover_agents(agents_dir=agents_dir)
        assert isinstance(agent_map, dict)

    def test_agent_map_has_lisa(self, agents_dir):
        """Agent map should contain a 'lisa' entry."""
        agent_map = config.discover_agents(agents_dir=agents_dir)
        assert "lisa" in agent_map

    def test_agent_entry_has_required_fields(self, agents_dir):
        """Each agent entry should have 'name', 'container', and 'role_file' keys."""
        agent_map = config.discover_agents(agents_dir=agents_dir)
        for agent_name, agent_config in agent_map.items():
            assert "name" in agent_config, f"Agent '{agent_name}' missing 'name'"
            assert "container" in agent_config, f"Agent '{agent_name}' missing 'container'"
            assert "role_file" in agent_config, f"Agent '{agent_name}' missing 'role_file'"

    def test_container_timeout_none_when_not_set(self, agents_dir):
        """container_timeout defaults to None when not in agent.yaml (falls back to global)."""
        agent_map = config.discover_agents(agents_dir=agents_dir)
        assert agent_map["lisa"]["container_timeout"] is None

    def test_container_timeout_read_from_yaml(self, tmp_path):
        """container_timeout from agent.yaml should be exposed in the agent dict."""
        agent_dir = tmp_path / "sam"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Sam
                container: sam
                container_timeout: 1800
            """)
        )
        agent_map = config.discover_agents(agents_dir=tmp_path)
        assert agent_map["sam"]["container_timeout"] == 1800

    def test_get_agent_map_uses_default_agents_dir(self, agents_dir, monkeypatch):
        """get_agent_map() should read from DEFAULT_AGENTS_DIR when no arg is given."""
        monkeypatch.setattr(config, "DEFAULT_AGENTS_DIR", agents_dir)
        config.reset_agent_map_cache()
        try:
            agent_map = config.get_agent_map()
            assert "lisa" in agent_map
        finally:
            config.reset_agent_map_cache()

    def test_discover_agents_falls_back_to_repo_config_when_default_missing(self, tmp_path, monkeypatch):
        """When DEFAULT_AGENTS_DIR doesn't exist (CI / local dev outside the container),
        discover_agents() should fall through to ``CONFIG_DIR/'agents'`` so the
        in-repo agent stubs are still picked up. Without this fall-through every
        caller that omits ``agents_dir=`` silently gets an empty map on CI,
        cascade-failing dozens of unrelated tests that call ``get_agent_map()``.
        """
        # DEFAULT_AGENTS_DIR points somewhere that doesn't exist.
        bogus_default = tmp_path / "does-not-exist"
        assert not bogus_default.exists()
        monkeypatch.setattr(config, "DEFAULT_AGENTS_DIR", bogus_default)

        # CONFIG_DIR/'agents' contains a stub agent.
        fake_repo_agents = tmp_path / "config" / "agents"
        stub = fake_repo_agents / "lisa"
        stub.mkdir(parents=True)
        (stub / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                thinking_status: ""
            """)
        )
        monkeypatch.setattr(config, "CONFIG_DIR", Path(tmp_path / "config"))

        agent_map = config.discover_agents()
        assert "lisa" in agent_map, (
            "Fall-through to CONFIG_DIR/'agents' broken — get_agent_map() will "
            "return {} on CI runners and cascade-fail every test that indexes "
            "the agent map by name."
        )


class TestEnvVarParsing:
    """Tests for environment variable loading with defaults."""

    @pytest.fixture(autouse=True)
    def patch_agents_dir(self, agents_dir, monkeypatch):
        """Point DEFAULT_AGENTS_DIR at the fixture and reset cache around each test."""
        monkeypatch.setattr(config, "DEFAULT_AGENTS_DIR", agents_dir)
        config.reset_agent_map_cache()
        yield
        config.reset_agent_map_cache()

    def test_slack_credentials_loaded_per_agent(self, monkeypatch):
        """Should read <NAME>_BOT_TOKEN/<NAME>_APP_TOKEN/<NAME>_SIGNING_SECRET per agent."""
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test-123")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-test-123")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "signing-test-123")
        cfg = config.load_config()
        assert "lisa" in cfg["slack_credentials"]
        assert cfg["slack_credentials"]["lisa"] == {
            "bot_token": "xoxb-test-123",
            "app_token": "xapp-test-123",
            "signing_secret": "signing-test-123",
        }

    def test_slack_credentials_skips_partial_agent(self, monkeypatch):
        """Agents missing any of the three creds should be skipped."""
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test-123")
        monkeypatch.delenv("LISA_APP_TOKEN", raising=False)
        monkeypatch.delenv("LISA_SIGNING_SECRET", raising=False)
        cfg = config.load_config()
        assert "lisa" not in cfg["slack_credentials"]

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

    def test_no_max_token_budget_in_config(self, monkeypatch):
        """The context token budget is owned by router.dispatcher (issue #144).

        load_config() must not expose ``max_token_budget`` — duplicating it
        across two layers is exactly what caused the production regression
        where ``config.py``'s stale 4000 default silently overrode the
        dispatcher's 32000.
        """
        monkeypatch.delenv("MAX_TOKEN_BUDGET", raising=False)
        monkeypatch.delenv("MAX_CONTEXT_TOKENS", raising=False)
        cfg = config.load_config()
        assert "max_token_budget" not in cfg
