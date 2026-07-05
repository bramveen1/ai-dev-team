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

    def test_dispatch_workspace_flag_defaults_false(self, agents_dir):
        """dispatch_workspace is surfaced from agent.yaml; absent → False."""
        agent_map = config.discover_agents(agents_dir=agents_dir)
        assert agent_map["lisa"]["dispatch_workspace"] is False

    def test_dispatch_workspace_flag_read_from_yaml(self, tmp_path):
        agent_dir = tmp_path / "nina"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: Nina\ncontainer: nina\ndispatch_workspace: true\n")
        agent_map = config.discover_agents(agents_dir=tmp_path)
        assert agent_map["nina"]["dispatch_workspace"] is True

    def test_known_agent_ids_derives_from_agent_map(self, monkeypatch):
        """known_agent_ids() mirrors discovery — the KNOWN_AGENTS frozensets are gone."""
        monkeypatch.setattr(config, "get_agent_map", lambda: {"lisa": {}, "nina": {}})
        assert config.known_agent_ids() == frozenset({"lisa", "nina"})

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


class TestSecretResolution:
    """Tests for ${SECRET:NAME} resolution."""

    def test_resolve_plain_string_unchanged(self):
        """Non-secret strings are returned as-is."""
        assert config.resolve_secret_ref("xoxb-hardcoded") == "xoxb-hardcoded"

    def test_resolve_secret_ref_from_env(self, monkeypatch):
        """${SECRET:FOO} is replaced with the FOO env var value."""
        monkeypatch.setenv("MY_TOKEN", "tok-abc")
        assert config.resolve_secret_ref("${SECRET:MY_TOKEN}") == "tok-abc"

    def test_resolve_secret_ref_raises_on_missing(self, monkeypatch):
        """${SECRET:MISSING} raises ValueError when the env var is absent."""
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        with pytest.raises(ValueError, match="MISSING_SECRET"):
            config.resolve_secret_ref("${SECRET:MISSING_SECRET}")

    def test_resolve_secret_ref_custom_env(self):
        """resolve_secret_ref accepts an explicit env dict."""
        env = {"MY_KEY": "resolved-value"}
        assert config.resolve_secret_ref("${SECRET:MY_KEY}", env=env) == "resolved-value"

    def test_resolve_secret_ref_partial_match_not_resolved(self):
        """A string that is not purely a ${SECRET:...} reference is returned unchanged."""
        assert config.resolve_secret_ref("prefix-${SECRET:FOO}") == "prefix-${SECRET:FOO}"


class TestBackendsBlock:
    """Tests for the backends: block in agent.yaml."""

    def test_backends_block_stored_in_agent_map(self, tmp_path):
        """discover_agents() stores the backends block in each agent entry."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                backends:
                  slack:
                    bot_token: ${SECRET:LISA_BOT_TOKEN}
                    app_token: ${SECRET:LISA_APP_TOKEN}
                    signing_secret: ${SECRET:LISA_SIGNING_SECRET}
            """)
        )
        agent_map = config.discover_agents(agents_dir=tmp_path)
        assert "backends" in agent_map["lisa"]
        assert "slack" in agent_map["lisa"]["backends"]

    def test_backends_empty_when_not_declared(self, tmp_path):
        """Agents without a backends: block get an empty dict."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: Lisa\ncontainer: lisa\n")
        agent_map = config.discover_agents(agents_dir=tmp_path)
        assert agent_map["lisa"]["backends"] == {}

    def test_credentials_loaded_from_backends_slack(self, tmp_path, monkeypatch):
        """load_slack_credentials() resolves ${SECRET:...} refs from backends.slack."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                backends:
                  slack:
                    bot_token: ${SECRET:LISA_BOT_TOKEN}
                    app_token: ${SECRET:LISA_APP_TOKEN}
                    signing_secret: ${SECRET:LISA_SIGNING_SECRET}
            """)
        )
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-from-backend")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-from-backend")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "secret-from-backend")

        agent_map = config.discover_agents(agents_dir=tmp_path)
        creds = config.load_slack_credentials(agent_map)

        assert creds["lisa"] == {
            "bot_token": "xoxb-from-backend",
            "app_token": "xapp-from-backend",
            "signing_secret": "secret-from-backend",
        }

    def test_backends_missing_secret_fails_loud(self, tmp_path, monkeypatch):
        """Unresolved ${SECRET:...} in backends.slack raises ValueError at credential load."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                backends:
                  slack:
                    bot_token: ${SECRET:LISA_BOT_TOKEN}
                    app_token: ${SECRET:LISA_APP_TOKEN}
                    signing_secret: ${SECRET:LISA_SIGNING_SECRET}
            """)
        )
        monkeypatch.delenv("LISA_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LISA_APP_TOKEN", raising=False)
        monkeypatch.delenv("LISA_SIGNING_SECRET", raising=False)

        agent_map = config.discover_agents(agents_dir=tmp_path)
        with pytest.raises(ValueError, match="LISA_BOT_TOKEN"):
            config.load_slack_credentials(agent_map)

    def test_backends_missing_required_field_fails_loud(self, tmp_path, monkeypatch):
        """backends.slack missing required fields raises ValueError."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                backends:
                  slack:
                    bot_token: ${SECRET:LISA_BOT_TOKEN}
            """)
        )
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test")

        agent_map = config.discover_agents(agents_dir=tmp_path)
        with pytest.raises(ValueError, match="missing required fields"):
            config.load_slack_credentials(agent_map)

    def test_validate_backends_block_rejects_non_dict_top(self, tmp_path):
        """A backends block that is not a mapping fails at discovery."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: Lisa\ncontainer: lisa\nbackends: not-a-dict\n")
        with pytest.raises(ValueError, match="backends.*mapping"):
            config.discover_agents(agents_dir=tmp_path)

    def test_validate_backends_block_rejects_non_dict_backend(self, tmp_path):
        """A backend entry that is not a mapping fails at discovery."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: Lisa\ncontainer: lisa\nbackends:\n  slack: not-a-dict\n")
        with pytest.raises(ValueError, match="backends.slack.*mapping"):
            config.discover_agents(agents_dir=tmp_path)

    def test_validate_backends_block_rejects_non_string_value(self, tmp_path):
        """Non-string values inside a backend entry fail at discovery."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                backends:
                  slack:
                    bot_token: 12345
            """)
        )
        with pytest.raises(ValueError, match="backends.slack.bot_token.*string"):
            config.discover_agents(agents_dir=tmp_path)

    def test_fallback_to_env_vars_when_no_backends(self, tmp_path, monkeypatch):
        """Agents without backends.slack fall back to the legacy env-var convention."""
        agent_dir = tmp_path / "lisa"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text("name: Lisa\ncontainer: lisa\n")
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-legacy")
        monkeypatch.setenv("LISA_APP_TOKEN", "xapp-legacy")
        monkeypatch.setenv("LISA_SIGNING_SECRET", "secret-legacy")

        agent_map = config.discover_agents(agents_dir=tmp_path)
        creds = config.load_slack_credentials(agent_map)

        assert creds["lisa"] == {
            "bot_token": "xoxb-legacy",
            "app_token": "xapp-legacy",
            "signing_secret": "secret-legacy",
        }

    def test_backends_schema_version_constant_exists(self):
        """_BACKENDS_SCHEMA_VERSION is a positive integer so schema changes are traceable."""
        assert isinstance(config._BACKENDS_SCHEMA_VERSION, int)
        assert config._BACKENDS_SCHEMA_VERSION >= 1


class TestDiscordCredentials:
    """Tests for load_discord_credentials."""

    def _make_agents_dir(self, tmp_path, yaml_content: str) -> dict:
        agent_dir = tmp_path / "sam"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(yaml_content)
        return config.discover_agents(agents_dir=tmp_path)

    def test_load_from_backends_discord_block(self, tmp_path, monkeypatch):
        """Resolves bot_token from backends.discord via ${SECRET:} ref."""
        agent_map = self._make_agents_dir(
            tmp_path,
            textwrap.dedent("""\
                name: Sam
                container: sam
                backends:
                  discord:
                    bot_token: ${SECRET:SAM_DISCORD_BOT_TOKEN}
                    default_channel_id: "111222333444555666"
            """),
        )
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "discord-token-sam")
        creds = config.load_discord_credentials(agent_map)
        assert "sam" in creds
        assert creds["sam"]["bot_token"] == "discord-token-sam"
        assert creds["sam"]["default_channel_id"] == 111222333444555666

    def test_default_channel_id_defaults_to_zero(self, tmp_path, monkeypatch):
        """default_channel_id defaults to 0 when omitted from backends.discord."""
        agent_map = self._make_agents_dir(
            tmp_path,
            textwrap.dedent("""\
                name: Sam
                container: sam
                backends:
                  discord:
                    bot_token: ${SECRET:SAM_DISCORD_BOT_TOKEN}
            """),
        )
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "discord-token-sam")
        creds = config.load_discord_credentials(agent_map)
        assert creds["sam"]["default_channel_id"] == 0

    def test_env_fallback_when_no_backends_discord(self, tmp_path, monkeypatch):
        """Falls back to SAM_DISCORD_BOT_TOKEN env var when no backends.discord block."""
        agent_map = self._make_agents_dir(tmp_path, "name: Sam\ncontainer: sam\n")
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "env-discord-token")
        creds = config.load_discord_credentials(agent_map)
        assert "sam" in creds
        assert creds["sam"]["bot_token"] == "env-discord-token"

    def test_env_fallback_channel_id_cast(self, tmp_path, monkeypatch):
        """SAM_DISCORD_CHANNEL_ID env var is cast to int."""
        agent_map = self._make_agents_dir(tmp_path, "name: Sam\ncontainer: sam\n")
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "env-discord-token")
        monkeypatch.setenv("SAM_DISCORD_CHANNEL_ID", "987654321098765432")
        creds = config.load_discord_credentials(agent_map)
        assert creds["sam"]["default_channel_id"] == 987654321098765432

    def test_soft_skip_when_no_token(self, tmp_path, monkeypatch):
        """Agents with no Discord credentials are skipped (soft-skip, no error)."""
        agent_map = self._make_agents_dir(tmp_path, "name: Sam\ncontainer: sam\n")
        monkeypatch.delenv("SAM_DISCORD_BOT_TOKEN", raising=False)
        creds = config.load_discord_credentials(agent_map)
        assert "sam" not in creds

    def test_fail_loud_on_unresolvable_secret(self, tmp_path, monkeypatch):
        """A declared ${SECRET:} ref that can't be resolved raises ValueError."""
        agent_map = self._make_agents_dir(
            tmp_path,
            textwrap.dedent("""\
                name: Sam
                container: sam
                backends:
                  discord:
                    bot_token: ${SECRET:SAM_DISCORD_BOT_TOKEN}
            """),
        )
        monkeypatch.delenv("SAM_DISCORD_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="SAM_DISCORD_BOT_TOKEN"):
            config.load_discord_credentials(agent_map)

    def test_missing_bot_token_field_fails_loud(self, tmp_path, monkeypatch):
        """backends.discord without bot_token raises ValueError."""
        agent_map = self._make_agents_dir(
            tmp_path,
            textwrap.dedent("""\
                name: Sam
                container: sam
                backends:
                  discord:
                    default_channel_id: "123"
            """),
        )
        with pytest.raises(ValueError, match="bot_token"):
            config.load_discord_credentials(agent_map)

    def test_returns_empty_dict_when_no_agents(self):
        """Returns an empty dict when agent_map is empty."""
        assert config.load_discord_credentials({}) == {}

    def test_env_fallback_default_channel_zero_when_missing(self, tmp_path, monkeypatch):
        """default_channel_id defaults to 0 when SAM_DISCORD_CHANNEL_ID is unset."""
        agent_map = self._make_agents_dir(tmp_path, "name: Sam\ncontainer: sam\n")
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "env-discord-token")
        monkeypatch.delenv("SAM_DISCORD_CHANNEL_ID", raising=False)
        creds = config.load_discord_credentials(agent_map)
        assert creds["sam"]["default_channel_id"] == 0


class TestDefaultAgentResolvers:
    """resolve_default_agent / resolve_worker_agent — no hardcoded personas."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for key in ("DEFAULT_AGENT", "AUTO_DISPATCH_WORKER_AGENT"):
            monkeypatch.delenv(key, raising=False)

    def test_default_agent_setting_wins(self, monkeypatch):
        monkeypatch.setattr(config, "get_agent_map", lambda: {"lisa": {}, "sam": {}})
        monkeypatch.setenv("DEFAULT_AGENT", "sam")
        assert config.resolve_default_agent() == "sam"

    def test_default_agent_falls_back_to_first_discovered(self, monkeypatch):
        monkeypatch.setattr(config, "get_agent_map", lambda: {"sam": {}, "lisa": {}})
        assert config.resolve_default_agent() == "lisa"

    def test_default_agent_stale_setting_warns_and_falls_back(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "get_agent_map", lambda: {"lisa": {}})
        monkeypatch.setenv("DEFAULT_AGENT", "ghost")
        with caplog.at_level("WARNING"):
            assert config.resolve_default_agent() == "lisa"
        assert any("not a discovered agent" in r.message for r in caplog.records)

    def test_default_agent_no_agents_raises(self, monkeypatch):
        monkeypatch.setattr(config, "get_agent_map", lambda: {})
        with pytest.raises(ValueError, match="No agents discovered"):
            config.resolve_default_agent()

    def test_worker_agent_setting_wins(self, monkeypatch):
        monkeypatch.setattr(config, "get_agent_map", lambda: {"lisa": {}, "nina": {"dispatch_workspace": True}})
        monkeypatch.setenv("AUTO_DISPATCH_WORKER_AGENT", "lisa")
        assert config.resolve_worker_agent() == "lisa"

    def test_worker_agent_setting_unknown_raises(self, monkeypatch):
        monkeypatch.setattr(config, "get_agent_map", lambda: {"lisa": {}})
        monkeypatch.setenv("AUTO_DISPATCH_WORKER_AGENT", "ghost")
        with pytest.raises(ValueError, match="not a discovered agent"):
            config.resolve_worker_agent()

    def test_worker_agent_resolves_sole_dispatch_workspace_flag(self, monkeypatch):
        """NOT alphabetical-first: the workspace mount is what makes dispatch work."""
        monkeypatch.setattr(
            config,
            "get_agent_map",
            lambda: {"aaa": {}, "nina": {"dispatch_workspace": True}},
        )
        assert config.resolve_worker_agent() == "nina"

    def test_worker_agent_zero_flagged_raises(self, monkeypatch):
        monkeypatch.setattr(config, "get_agent_map", lambda: {"lisa": {}, "sam": {}})
        with pytest.raises(ValueError, match="dispatch_workspace"):
            config.resolve_worker_agent()

    def test_worker_agent_multiple_flagged_raises(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "get_agent_map",
            lambda: {"a": {"dispatch_workspace": True}, "b": {"dispatch_workspace": True}},
        )
        with pytest.raises(ValueError, match="2 agents declare"):
            config.resolve_worker_agent()


class TestStoreBackedCredentials:
    """Per-agent credentials from data/secrets.json (agent_credentials block) —
    written by the /config page, store-over-manifest-over-env, restart-class."""

    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path, monkeypatch):
        import json as _json

        self._secrets_path = tmp_path / "secrets.json"
        monkeypatch.setattr("router.packs.secret_store.SECRETS_PATH", self._secrets_path)
        self._write = lambda block: self._secrets_path.write_text(_json.dumps({"agent_credentials": block}))

    def test_complete_store_block_wins(self, monkeypatch):
        monkeypatch.setenv("NINA_BOT_TOKEN", "xoxb-from-env")
        self._write(
            {"nina": {"slack": {"bot_token": "xoxb-store", "app_token": "xapp-store", "signing_secret": "sig-store"}}}
        )
        creds = config.load_slack_credentials({"nina": {}})
        assert creds["nina"] == {"bot_token": "xoxb-store", "app_token": "xapp-store", "signing_secret": "sig-store"}

    def test_store_conflict_with_env_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("NINA_BOT_TOKEN", "xoxb-from-env")
        self._write(
            {"nina": {"slack": {"bot_token": "xoxb-store", "app_token": "xapp-store", "signing_secret": "sig-store"}}}
        )
        with caplog.at_level("WARNING"):
            config.load_slack_credentials({"nina": {}})
        assert any("secret store wins" in r.message for r in caplog.records)

    def test_incomplete_store_block_falls_through_to_env(self, monkeypatch, caplog):
        self._write({"nina": {"slack": {"bot_token": "xoxb-store-only"}}})
        monkeypatch.setenv("NINA_BOT_TOKEN", "xoxb-env")
        monkeypatch.setenv("NINA_APP_TOKEN", "xapp-env")
        monkeypatch.setenv("NINA_SIGNING_SECRET", "sig-env")
        with caplog.at_level("WARNING"):
            creds = config.load_slack_credentials({"nina": {}})
        assert creds["nina"]["bot_token"] == "xoxb-env"
        assert any("incomplete" in r.message for r in caplog.records)

    def test_corrupt_store_never_degrades_below_env(self, monkeypatch):
        self._secrets_path.write_text("{ not json")
        monkeypatch.setenv("NINA_BOT_TOKEN", "xoxb-env")
        monkeypatch.setenv("NINA_APP_TOKEN", "xapp-env")
        monkeypatch.setenv("NINA_SIGNING_SECRET", "sig-env")
        creds = config.load_slack_credentials({"nina": {}})
        assert creds["nina"]["bot_token"] == "xoxb-env"

    def test_discord_store_block_wins(self, monkeypatch):
        monkeypatch.delenv("NINA_DISCORD_BOT_TOKEN", raising=False)
        self._write({"nina": {"discord": {"bot_token": "discord-store", "default_channel_id": "1234"}}})
        creds = config.load_discord_credentials({"nina": {}})
        assert creds["nina"] == {"bot_token": "discord-store", "default_channel_id": 1234}

    def test_discord_store_bad_channel_id_defaults_zero(self, caplog):
        self._write({"nina": {"discord": {"bot_token": "discord-store", "default_channel_id": "not-a-number"}}})
        with caplog.at_level("WARNING"):
            creds = config.load_discord_credentials({"nina": {}})
        assert creds["nina"]["default_channel_id"] == 0
