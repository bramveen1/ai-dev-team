"""Unit tests for src.capabilities.loader — config loading and validation."""

import textwrap

import pytest

from capabilities.loader import ConfigError, get_agent_capabilities, load_config, load_providers
from capabilities.models import AgentCapabilities

pytestmark = pytest.mark.unit


@pytest.fixture
def valid_providers_yaml(tmp_path):
    """Write a minimal valid providers.yaml and return its path."""
    content = textwrap.dedent("""\
        providers:
          zoho-mcp:
            command: npx
            args: ["-y", "@zoho/zoho-mcp"]
            capabilities: [email]
            permission_scopes:
              email:
                read: "Mail.Read"
                send: "Mail.Send"
                draft-create: "Mail.Draft"
                draft-update: "Mail.Draft"
                draft-delete: "Mail.Draft"
                archive: "Mail.Archive"
            env_template:
              ZOHO_ACCOUNT: "{account}"
              ZOHO_API_KEY: "${ZOHO_API_KEY}"
          m365-mcp:
            command: npx
            args: ["-y", "@microsoft/m365-mcp"]
            capabilities: [email, calendar]
            permission_scopes:
              email:
                read: "Mail.Read"
                send: "Mail.Send"
                draft-create: "Mail.ReadWrite"
                draft-update: "Mail.ReadWrite"
                draft-delete: "Mail.ReadWrite"
              calendar:
                read: "Calendars.Read"
                propose: "Calendars.ReadWrite"
                book: "Calendars.ReadWrite"
            env_template:
              M365_ACCOUNT: "{account}"
              M365_ACCESS_TOKEN: "${M365_ACCESS_TOKEN}"
              M365_SCOPES: "{computed_scopes}"
    """)
    p = tmp_path / "providers.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def valid_capabilities_yaml(tmp_path, valid_providers_yaml):
    """Write a valid capabilities.yaml alongside the providers file and return its path."""
    content = textwrap.dedent("""\
        agents:
          lisa:
            agent: lisa
            capabilities:
              email:
                - instance: mine
                  provider: zoho-mcp
                  account: lisa@pathtohired.com
                  ownership: self
                  permissions:
                    - read
                    - send
                    - archive
                    - draft-create
                    - draft-update
                    - draft-delete
                - instance: bram
                  provider: m365-mcp
                  account: bram@pathtohired.com
                  ownership: delegate
                  permissions:
                    - read
                    - draft-create
                    - draft-update
                    - draft-delete
              calendar:
                - instance: bram
                  provider: m365-mcp
                  account: bram@pathtohired.com
                  ownership: delegate
                  permissions:
                    - read
                    - propose
    """)
    p = tmp_path / "capabilities.yaml"
    p.write_text(content)
    return p


class TestLoadProviders:
    """Tests for loading the provider registry."""

    def test_load_valid_providers(self, valid_providers_yaml):
        providers = load_providers(valid_providers_yaml)
        assert "zoho-mcp" in providers.providers
        assert "m365-mcp" in providers.providers

    def test_provider_has_expected_fields(self, valid_providers_yaml):
        providers = load_providers(valid_providers_yaml)
        zoho = providers.providers["zoho-mcp"]
        assert zoho.command == "npx"
        assert zoho.args == ["-y", "@zoho/zoho-mcp"]
        assert "email" in zoho.capabilities

    def test_missing_providers_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_providers(tmp_path / "nonexistent.yaml")

    def test_empty_providers_file_raises(self, tmp_path):
        p = tmp_path / "providers.yaml"
        p.write_text("")
        with pytest.raises(ConfigError, match="'providers' key"):
            load_providers(p)


class TestLoadConfig:
    """Tests for loading and validating capabilities.yaml."""

    def test_load_valid_config_returns_dict(self, valid_capabilities_yaml):
        agents = load_config(valid_capabilities_yaml)
        assert isinstance(agents, dict)
        assert "lisa" in agents

    def test_valid_config_roundtrip(self, valid_capabilities_yaml):
        """Load config, verify structure matches expected data."""
        agents = load_config(valid_capabilities_yaml)
        lisa = agents["lisa"]
        assert isinstance(lisa, AgentCapabilities)
        assert lisa.agent == "lisa"
        assert "email" in lisa.capabilities
        assert "calendar" in lisa.capabilities
        assert len(lisa.capabilities["email"]) == 2
        assert len(lisa.capabilities["calendar"]) == 1

    def test_email_mine_instance(self, valid_capabilities_yaml):
        agents = load_config(valid_capabilities_yaml)
        email_instances = agents["lisa"].capabilities["email"]
        mine = next(i for i in email_instances if i.instance == "mine")
        assert mine.provider == "zoho-mcp"
        assert mine.account == "lisa@pathtohired.com"
        assert mine.ownership == "self"
        assert "send" in mine.permissions
        assert "read" in mine.permissions

    def test_email_bram_instance(self, valid_capabilities_yaml):
        agents = load_config(valid_capabilities_yaml)
        email_instances = agents["lisa"].capabilities["email"]
        bram = next(i for i in email_instances if i.instance == "bram")
        assert bram.provider == "m365-mcp"
        assert bram.ownership == "delegate"
        assert "send" not in bram.permissions

    def test_missing_config_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_empty_config_file_raises(self, tmp_path):
        (tmp_path / "providers.yaml").write_text("providers: {}")
        p = tmp_path / "capabilities.yaml"
        p.write_text("")
        with pytest.raises(ConfigError, match="'agents' key"):
            load_config(p)

    def test_unknown_provider_rejected(self, tmp_path, valid_providers_yaml):
        """Config referencing a provider not in the registry should fail."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  email:
                    - instance: mine
                      provider: nonexistent-mcp
                      account: lisa@test.com
                      ownership: self
                      permissions: [read]
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="unknown provider 'nonexistent-mcp'"):
            load_config(p)

    def test_invalid_permission_rejected(self, tmp_path, valid_providers_yaml):
        """Config with a permission not in the vocabulary should fail."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  email:
                    - instance: mine
                      provider: zoho-mcp
                      account: lisa@test.com
                      ownership: self
                      permissions: [read, teleport]
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="invalid permission 'teleport'"):
            load_config(p)

    def test_invalid_ownership_rejected(self, tmp_path, valid_providers_yaml):
        """Config with invalid ownership value should fail."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  email:
                    - instance: mine
                      provider: zoho-mcp
                      account: lisa@test.com
                      ownership: stolen
                      permissions: [read]
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="Invalid ownership 'stolen'"):
            load_config(p)

    def test_provider_capability_mismatch_rejected(self, tmp_path, valid_providers_yaml):
        """Using a provider for a capability it doesn't support should fail."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  calendar:
                    - instance: mine
                      provider: zoho-mcp
                      account: lisa@test.com
                      ownership: self
                      permissions: [read]
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="does not support capability 'calendar'"):
            load_config(p)

    def test_duplicate_instance_name_rejected(self, tmp_path, valid_providers_yaml):
        """Two instances with the same name in one capability type should fail."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  email:
                    - instance: mine
                      provider: zoho-mcp
                      account: lisa@pathtohired.com
                      ownership: self
                      permissions: [read]
                    - instance: mine
                      provider: zoho-mcp
                      account: other@pathtohired.com
                      ownership: self
                      permissions: [read]
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="duplicate instance name 'mine'"):
            load_config(p)

    def test_missing_required_field_rejected(self, tmp_path, valid_providers_yaml):
        """Instance missing a required field (e.g. account) should fail."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  email:
                    - instance: mine
                      provider: zoho-mcp
                      ownership: self
                      permissions: [read]
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="Invalid config for agent"):
            load_config(p)


class TestGetAgentCapabilities:
    """Tests for the get_agent_capabilities convenience function."""

    def test_returns_agent_caps(self, valid_capabilities_yaml):
        caps = get_agent_capabilities("lisa", valid_capabilities_yaml)
        assert isinstance(caps, AgentCapabilities)
        assert caps.agent == "lisa"

    def test_unknown_agent_raises(self, valid_capabilities_yaml):
        with pytest.raises(ConfigError, match="not found in capabilities config"):
            get_agent_capabilities("nonexistent", valid_capabilities_yaml)


class TestRealConfig:
    """Tests that verify the actual config/capabilities.yaml and config/providers.yaml load correctly."""

    def test_real_providers_load(self):
        """The checked-in providers.yaml should load without errors."""
        providers = load_providers()
        assert len(providers.providers) > 0

    def test_real_config_loads(self):
        """The checked-in capabilities.yaml should load without errors."""
        agents = load_config()
        assert "lisa" in agents

    def test_real_lisa_has_email_and_calendar(self):
        """Lisa should have email and calendar capabilities in the real config."""
        caps = get_agent_capabilities("lisa")
        assert "email" in caps.capabilities
        assert "calendar" in caps.capabilities

    def test_real_providers_include_connectors(self):
        """The checked-in providers.yaml should include connector-based providers."""
        providers = load_providers()
        assert "m365-connector" in providers.providers
        assert "gmail-connector" in providers.providers
        assert "gcal-connector" in providers.providers
        assert "gdrive-connector" in providers.providers

    def test_real_connector_provider_has_transport_field(self):
        """Connector providers should have transport='connector'."""
        providers = load_providers()
        m365 = providers.providers["m365-connector"]
        assert m365.transport == "connector"

    def test_real_command_provider_has_default_transport(self):
        """Command providers should default to transport='command'."""
        providers = load_providers()
        zoho = providers.providers["zoho-mcp"]
        assert zoho.transport == "command"
