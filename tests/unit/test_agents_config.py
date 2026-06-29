"""Unit tests for router.agents_config — agents.yaml schema, secret resolution, validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

agents_config = pytest.importorskip("router.agents_config", reason="router.agents_config not yet implemented")

pytestmark = pytest.mark.unit

AgentsConfigError = agents_config.AgentsConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_agents_yaml(path: Path, content: str) -> Path:
    """Write YAML content to a file and return the path."""
    p = path / "agents.yaml"
    p.write_text(textwrap.dedent(content))
    return p


@pytest.fixture
def minimal_yaml(tmp_path):
    """Minimal valid agents.yaml with one agent and a Slack backend."""
    return _write_agents_yaml(
        tmp_path,
        """\
        version: "1"
        agents:
          - id: lisa
            name: Lisa
            backends:
              slack:
                bot_token: "${SECRET:LISA_BOT_TOKEN}"
                app_token: "${SECRET:LISA_APP_TOKEN}"
                signing_secret: "${SECRET:LISA_SIGNING_SECRET}"
        """,
    )


@pytest.fixture
def two_agent_yaml(tmp_path):
    """Valid agents.yaml with Lisa (Slack) and Sam (Slack + future Discord stub)."""
    return _write_agents_yaml(
        tmp_path,
        """\
        version: "1"
        agents:
          - id: lisa
            name: Lisa
            backends:
              slack:
                bot_token: "${SECRET:LISA_BOT_TOKEN}"
                app_token: "${SECRET:LISA_APP_TOKEN}"
                signing_secret: "${SECRET:LISA_SIGNING_SECRET}"
          - id: sam
            name: Sam
            backends:
              slack:
                bot_token: "${SECRET:SAM_BOT_TOKEN}"
                app_token: "${SECRET:SAM_APP_TOKEN}"
                signing_secret: "${SECRET:SAM_SIGNING_SECRET}"
        """,
    )


@pytest.fixture
def resolver_all_set():
    """A resolver that returns a predictable value for every key."""

    def _resolve(name: str) -> str | None:
        return f"resolved-{name}"

    return _resolve


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------


class TestResolveSecretRefs:
    def test_no_placeholders_unchanged(self):
        result = agents_config.resolve_secret_refs("plain-value")
        assert result == "plain-value"

    def test_single_placeholder_resolved(self):
        result = agents_config.resolve_secret_refs("${SECRET:MY_VAR}", resolver=lambda _: "my-value")
        assert result == "my-value"

    def test_placeholder_in_the_middle(self):
        result = agents_config.resolve_secret_refs(
            "prefix-${SECRET:FOO}-suffix",
            resolver=lambda _: "bar",
        )
        assert result == "prefix-bar-suffix"

    def test_multiple_placeholders(self):
        mapping = {"A": "alpha", "B": "beta"}
        result = agents_config.resolve_secret_refs(
            "${SECRET:A}:${SECRET:B}",
            resolver=mapping.get,
        )
        assert result == "alpha:beta"

    def test_missing_secret_raises(self):
        with pytest.raises(AgentsConfigError, match="MY_MISSING"):
            agents_config.resolve_secret_refs("${SECRET:MY_MISSING}", resolver=lambda _: None)

    def test_uses_env_vars_by_default(self, monkeypatch):
        monkeypatch.setenv("INLINE_TEST_VAR", "hello")
        result = agents_config.resolve_secret_refs("${SECRET:INLINE_TEST_VAR}")
        assert result == "hello"


# ---------------------------------------------------------------------------
# Schema validation — valid configs
# ---------------------------------------------------------------------------


class TestLoadAgentsYaml:
    def test_load_minimal_valid(self, minimal_yaml):
        data = agents_config.load_agents_yaml(minimal_yaml)
        assert data["version"] == "1"
        assert len(data["agents"]) == 1
        assert data["agents"][0]["id"] == "lisa"

    def test_load_two_agents(self, two_agent_yaml):
        data = agents_config.load_agents_yaml(two_agent_yaml)
        ids = [a["id"] for a in data["agents"]]
        assert ids == ["lisa", "sam"]

    def test_agent_without_backends_is_valid(self, tmp_path):
        """Backends block is optional — agent may have no backend bindings yet."""
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: dave
                name: Dave
            """,
        )
        data = agents_config.load_agents_yaml(path)
        assert data["agents"][0]["id"] == "dave"

    def test_discord_backend_valid(self, tmp_path):
        """An agent can declare a discord backend without Slack."""
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: sam
                backends:
                  discord:
                    bot_token: "${SECRET:SAM_DISCORD_TOKEN}"
            """,
        )
        data = agents_config.load_agents_yaml(path)
        assert "discord" in data["agents"][0]["backends"]

    def test_secret_refs_are_not_resolved_on_load(self, minimal_yaml):
        """load_agents_yaml() returns raw (unresolved) values."""
        data = agents_config.load_agents_yaml(minimal_yaml)
        slack = data["agents"][0]["backends"]["slack"]
        assert slack["bot_token"] == "${SECRET:LISA_BOT_TOKEN}"


# ---------------------------------------------------------------------------
# Schema validation — invalid configs
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(AgentsConfigError, match="not found"):
            agents_config.load_agents_yaml(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_raises(self, tmp_path):
        bad = tmp_path / "agents.yaml"
        bad.write_text("version: [unclosed")
        with pytest.raises(AgentsConfigError, match="parse"):
            agents_config.load_agents_yaml(bad)

    def test_wrong_version_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "2"
            agents: []
            """,
        )
        with pytest.raises(AgentsConfigError, match="version"):
            agents_config.load_agents_yaml(path)

    def test_missing_version_raises(self, tmp_path):
        path = _write_agents_yaml(tmp_path, "agents: []\n")
        with pytest.raises(AgentsConfigError, match="version"):
            agents_config.load_agents_yaml(path)

    def test_agents_not_list_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents: not-a-list
            """,
        )
        with pytest.raises(AgentsConfigError, match="list"):
            agents_config.load_agents_yaml(path)

    def test_agent_missing_id_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - name: NoId
            """,
        )
        with pytest.raises(AgentsConfigError, match="id"):
            agents_config.load_agents_yaml(path)

    def test_duplicate_agent_id_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: lisa
              - id: lisa
            """,
        )
        with pytest.raises(AgentsConfigError, match="duplicate"):
            agents_config.load_agents_yaml(path)

    def test_slack_missing_bot_token_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: lisa
                backends:
                  slack:
                    app_token: xapp-x
                    signing_secret: sig-x
            """,
        )
        with pytest.raises(AgentsConfigError, match="bot_token"):
            agents_config.load_agents_yaml(path)

    def test_slack_missing_app_token_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: lisa
                backends:
                  slack:
                    bot_token: xoxb-x
                    signing_secret: sig-x
            """,
        )
        with pytest.raises(AgentsConfigError, match="app_token"):
            agents_config.load_agents_yaml(path)

    def test_slack_missing_signing_secret_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: lisa
                backends:
                  slack:
                    bot_token: xoxb-x
                    app_token: xapp-x
            """,
        )
        with pytest.raises(AgentsConfigError, match="signing_secret"):
            agents_config.load_agents_yaml(path)

    def test_discord_missing_bot_token_raises(self, tmp_path):
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: sam
                backends:
                  discord: {}
            """,
        )
        with pytest.raises(AgentsConfigError, match="bot_token"):
            agents_config.load_agents_yaml(path)

    def test_root_not_mapping_raises(self, tmp_path):
        bad = tmp_path / "agents.yaml"
        bad.write_text("- a list at root\n")
        with pytest.raises(AgentsConfigError, match="mapping"):
            agents_config.load_agents_yaml(bad)


# ---------------------------------------------------------------------------
# Credential extraction
# ---------------------------------------------------------------------------


class TestGetSlackCredentials:
    def test_resolves_all_fields(self, minimal_yaml, resolver_all_set):
        data = agents_config.load_agents_yaml(minimal_yaml)
        creds = agents_config.get_slack_credentials(data, resolver_all_set)
        assert "lisa" in creds
        assert creds["lisa"]["bot_token"] == "resolved-LISA_BOT_TOKEN"
        assert creds["lisa"]["app_token"] == "resolved-LISA_APP_TOKEN"
        assert creds["lisa"]["signing_secret"] == "resolved-LISA_SIGNING_SECRET"

    def test_skips_agent_with_no_slack_backend(self, two_agent_yaml, resolver_all_set):
        """An agent that only has discord (no slack block) is skipped."""
        data = agents_config.load_agents_yaml(two_agent_yaml)
        # Remove slack from sam to simulate discord-only
        data["agents"][1]["backends"].pop("slack")
        creds = agents_config.get_slack_credentials(data, resolver_all_set)
        assert "sam" not in creds
        assert "lisa" in creds

    def test_skips_agent_when_secret_missing(self, minimal_yaml):
        """Agent is soft-skipped when a secret cannot be resolved."""
        data = agents_config.load_agents_yaml(minimal_yaml)
        creds = agents_config.get_slack_credentials(data, resolver=lambda _: None)
        assert "lisa" not in creds

    def test_two_agents_resolved_independently(self, two_agent_yaml):
        """Each agent's secrets are resolved independently; missing one doesn't block the other."""
        mapping = {
            "LISA_BOT_TOKEN": "xoxb-lisa",
            "LISA_APP_TOKEN": "xapp-lisa",
            "LISA_SIGNING_SECRET": "sig-lisa",
            # SAM secrets are absent — sam should be skipped
        }
        data = agents_config.load_agents_yaml(two_agent_yaml)
        creds = agents_config.get_slack_credentials(data, resolver=mapping.get)
        assert "lisa" in creds
        assert "sam" not in creds

    def test_empty_resolved_value_skips_agent(self, tmp_path):
        """Empty string after resolution (resolver returns '') skips the agent."""
        path = _write_agents_yaml(
            tmp_path,
            """\
            version: "1"
            agents:
              - id: lisa
                backends:
                  slack:
                    bot_token: "${SECRET:EMPTY}"
                    app_token: "${SECRET:LISA_APP}"
                    signing_secret: "${SECRET:LISA_SIG}"
            """,
        )
        data = agents_config.load_agents_yaml(path)
        mapping = {"EMPTY": "", "LISA_APP": "xapp", "LISA_SIG": "sig"}
        creds = agents_config.get_slack_credentials(data, resolver=mapping.get)
        assert "lisa" not in creds

    def test_uses_env_vars_when_no_resolver(self, minimal_yaml, monkeypatch):
        """Without an explicit resolver, ${SECRET:…} is resolved from env vars."""
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-env")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-env")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "sig-env")
        data = agents_config.load_agents_yaml(minimal_yaml)
        creds = agents_config.get_slack_credentials(data)
        assert creds["lisa"]["bot_token"] == "xoxb-env"


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


class TestLoadSlackCredentialsFromYaml:
    def test_combines_load_and_resolve(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-wrap")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-wrap")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "sig-wrap")
        creds = agents_config.load_slack_credentials_from_yaml(minimal_yaml)
        assert creds["lisa"]["bot_token"] == "xoxb-wrap"

    def test_raises_on_bad_schema(self, tmp_path):
        bad = _write_agents_yaml(tmp_path, "version: '2'\nagents: []\n")
        with pytest.raises(AgentsConfigError, match="version"):
            agents_config.load_slack_credentials_from_yaml(bad)


# ---------------------------------------------------------------------------
# Integration: config.load_config uses agents.yaml when present
# ---------------------------------------------------------------------------


class TestLoadConfigIntegration:
    """Verify that router.config.load_config() picks up agents.yaml."""

    def test_load_config_uses_agents_yaml(self, minimal_yaml, monkeypatch):
        """When AGENTS_CONFIG points at a valid file, load_config uses it."""
        import textwrap

        config = pytest.importorskip("router.config")

        # Point at minimal_yaml (one lisa agent)
        monkeypatch.setenv("AGENTS_CONFIG", str(minimal_yaml))

        # Provide real secrets for resolution
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-intg")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-intg")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "sig-intg")

        # Make agent_map aware of lisa via a tmp agents dir
        agents_dir = minimal_yaml.parent / "agents"
        agents_dir.mkdir()
        lisa_dir = agents_dir / "lisa"
        lisa_dir.mkdir()
        (lisa_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                thinking_status: ""
            """)
        )
        monkeypatch.setattr(config, "DEFAULT_AGENTS_DIR", agents_dir)
        config.reset_agent_map_cache()

        try:
            cfg = config.load_config()
        finally:
            config.reset_agent_map_cache()
            monkeypatch.delenv("AGENTS_CONFIG", raising=False)

        assert "lisa" in cfg["slack_credentials"]
        assert cfg["slack_credentials"]["lisa"]["bot_token"] == "xoxb-intg"

    def test_load_config_falls_back_to_env_vars_when_no_yaml(self, tmp_path, monkeypatch):
        """Without an agents.yaml file, load_config falls back to env-var credentials."""
        config = pytest.importorskip("router.config")

        # No agents.yaml at AGENTS_CONFIG path
        monkeypatch.setenv("AGENTS_CONFIG", str(tmp_path / "nonexistent.yaml"))

        # Provide env-var credentials (flat approach)
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-fallback")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-fallback")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "sig-fallback")

        import textwrap

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        lisa_dir = agents_dir / "lisa"
        lisa_dir.mkdir()
        (lisa_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                thinking_status: ""
            """)
        )
        monkeypatch.setattr(config, "DEFAULT_AGENTS_DIR", agents_dir)
        config.reset_agent_map_cache()

        try:
            cfg = config.load_config()
        finally:
            config.reset_agent_map_cache()
            monkeypatch.delenv("AGENTS_CONFIG", raising=False)

        assert "lisa" in cfg["slack_credentials"]
        assert cfg["slack_credentials"]["lisa"]["bot_token"] == "xoxb-fallback"
