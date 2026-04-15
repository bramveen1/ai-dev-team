"""Unit tests for src.capabilities.mcp_namespacer — MCP config generation."""

import textwrap

import pytest

from capabilities.mcp_namespacer import generate_mcp_config

pytestmark = pytest.mark.unit


@pytest.fixture
def providers_yaml(tmp_path):
    """Write a providers.yaml and return its path."""
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
def capabilities_yaml(tmp_path, providers_yaml):
    """Write a capabilities.yaml and return its path."""
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


class TestGenerateMcpConfig:
    """Tests for MCP config generation."""

    def test_returns_mcp_servers_key(self, capabilities_yaml, providers_yaml):
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        assert "mcpServers" in result

    def test_correct_namespaces(self, capabilities_yaml, providers_yaml):
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        servers = result["mcpServers"]
        assert "email_mine" in servers
        assert "email_bram" in servers
        assert "calendar_bram" in servers
        assert len(servers) == 3

    def test_email_mine_server_config(self, capabilities_yaml, providers_yaml):
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        email_mine = result["mcpServers"]["email_mine"]
        assert email_mine["command"] == "npx"
        assert email_mine["args"] == ["-y", "@zoho/zoho-mcp"]
        assert email_mine["env"]["ZOHO_ACCOUNT"] == "lisa@pathtohired.com"
        assert email_mine["env"]["ZOHO_API_KEY"] == "${ZOHO_API_KEY}"

    def test_email_bram_server_config(self, capabilities_yaml, providers_yaml):
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        email_bram = result["mcpServers"]["email_bram"]
        assert email_bram["command"] == "npx"
        assert email_bram["args"] == ["-y", "@microsoft/m365-mcp"]
        assert email_bram["env"]["M365_ACCOUNT"] == "bram@pathtohired.com"
        assert email_bram["env"]["M365_ACCESS_TOKEN"] == "${M365_ACCESS_TOKEN}"

    def test_computed_scopes_for_email_bram(self, capabilities_yaml, providers_yaml):
        """email_bram has read + draft permissions, scopes should be Mail.Read,Mail.ReadWrite."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        scopes = result["mcpServers"]["email_bram"]["env"]["M365_SCOPES"]
        assert "Mail.Read" in scopes
        assert "Mail.ReadWrite" in scopes

    def test_computed_scopes_deduplication(self, capabilities_yaml, providers_yaml):
        """Scopes should be deduplicated — draft-create/update/delete all map to Mail.ReadWrite."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        scopes = result["mcpServers"]["email_bram"]["env"]["M365_SCOPES"]
        scope_list = scopes.split(",")
        assert len(scope_list) == len(set(scope_list)), f"Duplicate scopes found: {scope_list}"

    def test_calendar_bram_scopes(self, capabilities_yaml, providers_yaml):
        """calendar_bram has read + propose, scopes should include Calendars.Read,Calendars.ReadWrite."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        scopes = result["mcpServers"]["calendar_bram"]["env"]["M365_SCOPES"]
        assert "Calendars.Read" in scopes
        assert "Calendars.ReadWrite" in scopes

    def test_no_namespace_collisions(self, capabilities_yaml, providers_yaml):
        """All namespace keys should be unique."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        namespaces = list(result["mcpServers"].keys())
        assert len(namespaces) == len(set(namespaces))

    def test_env_vars_preserved_as_references(self, capabilities_yaml, providers_yaml):
        """${VAR} references should be kept as-is for runtime resolution."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        assert result["mcpServers"]["email_mine"]["env"]["ZOHO_API_KEY"] == "${ZOHO_API_KEY}"
        assert result["mcpServers"]["email_bram"]["env"]["M365_ACCESS_TOKEN"] == "${M365_ACCESS_TOKEN}"


class TestRealMcpConfig:
    """Tests using the actual config files."""

    def test_real_lisa_mcp_config(self):
        """The checked-in config should produce a valid MCP config for Lisa."""
        result = generate_mcp_config("lisa")
        assert "mcpServers" in result
        assert "email_mine" in result["mcpServers"]
        assert "email_bram" in result["mcpServers"]
        assert "calendar_bram" in result["mcpServers"]
