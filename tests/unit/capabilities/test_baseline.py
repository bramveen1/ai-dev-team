"""Unit tests for baseline capability loading and merging."""

import textwrap

import pytest

from src.capabilities.loader import get_agent_capabilities, load_baseline, load_config
from src.capabilities.mcp_namespacer import generate_mcp_config
from src.capabilities.prompt_renderer import render_capability_summary

pytestmark = pytest.mark.unit


@pytest.fixture
def providers_yaml(tmp_path):
    """Write a providers.yaml with baseline + agent providers."""
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
          playwright-mcp:
            command: npx
            args: ["-y", "@anthropic/playwright-mcp"]
            capabilities: [web]
            permission_scopes:
              web:
                browse: "web:read"
                screenshot: "web:read"
                interact: "web:write"
            env_template: {}
          memory-mcp:
            command: npx
            args: ["-y", "@anthropic/memory-mcp"]
            capabilities: [memory]
            permission_scopes:
              memory:
                read: "fs:read"
                write: "fs:write"
            env_template:
              MEMORY_ROOT: "${MEMORY_ROOT}"
              MEMORY_SCOPE: "{account}"
          slack-mcp:
            command: npx
            args: ["-y", "@anthropic/slack-mcp"]
            capabilities: [slack_io]
            permission_scopes:
              slack_io:
                read: "channels:read,groups:read"
                send: "chat:write"
                react: "reactions:write"
            env_template:
              SLACK_BOT_TOKEN: "${SLACK_BOT_TOKEN}"
              SLACK_WORKSPACE: "{account}"
          scheduler-mcp:
            command: npx
            args: ["-y", "@anthropic/scheduler-mcp"]
            capabilities: [scheduled_tasks]
            permission_scopes:
              scheduled_tasks:
                create: "schedule:write"
                read: "schedule:read"
                update: "schedule:write"
                delete: "schedule:write"
            env_template:
              SCHEDULER_AGENT: "{account}"
              SCHEDULER_STORE: "${SCHEDULER_STORE}"
    """)
    p = tmp_path / "providers.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def baseline_yaml(tmp_path, providers_yaml):
    """Write a baseline.yaml with web, memory, slack_io, scheduled_tasks."""
    content = textwrap.dedent("""\
        capabilities:
          web:
            - instance: browser
              provider: playwright-mcp
              account: shared
              ownership: shared
              permissions:
                - browse
                - screenshot
                - interact
          memory:
            - instance: agent
              provider: memory-mcp
              account: "{agent}"
              ownership: self
              permissions:
                - read
                - write
            - instance: shared
              provider: memory-mcp
              account: shared
              ownership: shared
              permissions:
                - read
          slack_io:
            - instance: team
              provider: slack-mcp
              account: pathtohired
              ownership: shared
              permissions:
                - read
                - send
                - react
          scheduled_tasks:
            - instance: agent
              provider: scheduler-mcp
              account: "{agent}"
              ownership: self
              permissions:
                - create
                - read
                - update
                - delete
    """)
    p = tmp_path / "baseline.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def capabilities_yaml(tmp_path, providers_yaml, baseline_yaml):
    """Write a capabilities.yaml that only has agent-specific caps."""
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
    """)
    p = tmp_path / "capabilities.yaml"
    p.write_text(content)
    return p


class TestLoadBaseline:
    """Tests for loading baseline.yaml."""

    def test_load_existing_baseline(self, baseline_yaml):
        baseline = load_baseline(baseline_yaml)
        assert "web" in baseline
        assert "memory" in baseline
        assert "slack_io" in baseline
        assert "scheduled_tasks" in baseline

    def test_missing_baseline_returns_empty(self, tmp_path):
        baseline = load_baseline(tmp_path / "nonexistent.yaml")
        assert baseline == {}

    def test_empty_baseline_returns_empty(self, tmp_path):
        p = tmp_path / "baseline.yaml"
        p.write_text("")
        baseline = load_baseline(p)
        assert baseline == {}

    def test_baseline_has_correct_instance_count(self, baseline_yaml):
        baseline = load_baseline(baseline_yaml)
        assert len(baseline["web"]) == 1
        assert len(baseline["memory"]) == 2
        assert len(baseline["slack_io"]) == 1
        assert len(baseline["scheduled_tasks"]) == 1


class TestBaselineMerging:
    """Tests for merging baseline capabilities into agent configs."""

    def test_baseline_caps_added_to_agent(self, capabilities_yaml):
        """Agent should get baseline capabilities merged in."""
        agents = load_config(capabilities_yaml)
        lisa = agents["lisa"]
        # Agent-specific
        assert "email" in lisa.capabilities
        # From baseline
        assert "web" in lisa.capabilities
        assert "memory" in lisa.capabilities
        assert "slack_io" in lisa.capabilities
        assert "scheduled_tasks" in lisa.capabilities

    def test_agent_specific_caps_preserved(self, capabilities_yaml):
        """Agent-specific capabilities should be untouched."""
        agents = load_config(capabilities_yaml)
        lisa = agents["lisa"]
        email_instances = lisa.capabilities["email"]
        assert len(email_instances) == 1
        assert email_instances[0].instance == "mine"
        assert email_instances[0].provider == "zoho-mcp"

    def test_agent_placeholder_resolved(self, capabilities_yaml):
        """The {agent} placeholder in baseline account should be resolved to agent name."""
        agents = load_config(capabilities_yaml)
        lisa = agents["lisa"]
        # Memory agent instance should have account = "lisa"
        memory_instances = lisa.capabilities["memory"]
        agent_inst = next(i for i in memory_instances if i.instance == "agent")
        assert agent_inst.account == "lisa"
        # Shared instance should keep "shared"
        shared_inst = next(i for i in memory_instances if i.instance == "shared")
        assert shared_inst.account == "shared"

    def test_scheduled_tasks_placeholder_resolved(self, capabilities_yaml):
        """Scheduled tasks agent placeholder should resolve."""
        agents = load_config(capabilities_yaml)
        lisa = agents["lisa"]
        sched_instances = lisa.capabilities["scheduled_tasks"]
        assert sched_instances[0].account == "lisa"

    def test_total_capability_count(self, capabilities_yaml):
        """Lisa should have 5 capability types: email + 4 from baseline."""
        agents = load_config(capabilities_yaml)
        lisa = agents["lisa"]
        assert len(lisa.capabilities) == 5

    def test_agent_override_wins(self, tmp_path, providers_yaml, baseline_yaml):
        """If agent declares same cap_type + instance as baseline, agent wins."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  web:
                    - instance: browser
                      provider: playwright-mcp
                      account: lisa-custom
                      ownership: self
                      permissions:
                        - browse
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)

        agents = load_config(p)
        lisa = agents["lisa"]
        web_instances = lisa.capabilities["web"]
        # Only one instance — agent's override
        browser = next(i for i in web_instances if i.instance == "browser")
        assert browser.account == "lisa-custom"
        assert browser.ownership == "self"
        assert browser.permissions == ["browse"]

    def test_baseline_instance_added_alongside_agent_instance(self, tmp_path, providers_yaml, baseline_yaml):
        """If agent has a different instance name for same cap_type, both should exist."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  web:
                    - instance: custom
                      provider: playwright-mcp
                      account: lisa-custom
                      ownership: self
                      permissions:
                        - browse
        """)
        p = tmp_path / "capabilities.yaml"
        p.write_text(content)

        agents = load_config(p)
        lisa = agents["lisa"]
        web_instances = lisa.capabilities["web"]
        instance_names = {i.instance for i in web_instances}
        assert "custom" in instance_names
        assert "browser" in instance_names
        assert len(web_instances) == 2

    def test_no_baseline_file_still_works(self, tmp_path, providers_yaml):
        """Config should load fine without a baseline.yaml."""
        content = textwrap.dedent("""\
            agents:
              lisa:
                agent: lisa
                capabilities:
                  web:
                    - instance: browser
                      provider: playwright-mcp
                      account: shared
                      ownership: shared
                      permissions:
                        - browse
        """)
        # Remove baseline if it exists
        baseline = tmp_path / "baseline.yaml"
        if baseline.exists():
            baseline.unlink()

        p = tmp_path / "capabilities.yaml"
        p.write_text(content)

        agents = load_config(p)
        assert "lisa" in agents
        assert len(agents["lisa"].capabilities) == 1


class TestBaselineMcpConfig:
    """Tests that baseline capabilities generate correct MCP server entries."""

    def test_baseline_caps_in_mcp_config(self, capabilities_yaml, providers_yaml):
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        servers = result["mcpServers"]
        # Agent-specific
        assert "email_mine" in servers
        # From baseline
        assert "web_browser" in servers
        assert "memory_agent" in servers
        assert "memory_shared" in servers
        assert "slack_io_team" in servers
        assert "scheduled_tasks_agent" in servers

    def test_memory_agent_scoped_env(self, capabilities_yaml, providers_yaml):
        """Memory agent instance should have account resolved to agent name."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        memory_agent = result["mcpServers"]["memory_agent"]
        assert memory_agent["env"]["MEMORY_SCOPE"] == "lisa"

    def test_memory_shared_env(self, capabilities_yaml, providers_yaml):
        """Memory shared instance should have account = 'shared'."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        memory_shared = result["mcpServers"]["memory_shared"]
        assert memory_shared["env"]["MEMORY_SCOPE"] == "shared"

    def test_no_namespace_collisions_with_baseline(self, capabilities_yaml, providers_yaml):
        """All namespace keys should remain unique after baseline merge."""
        result = generate_mcp_config("lisa", capabilities_yaml, providers_yaml)
        namespaces = list(result["mcpServers"].keys())
        assert len(namespaces) == len(set(namespaces))


class TestBaselinePromptRenderer:
    """Tests that baseline capabilities appear in prompt rendering."""

    def test_baseline_caps_in_summary(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "### web" in summary
        assert "### memory" in summary
        assert "### slack_io" in summary
        assert "### scheduled_tasks" in summary
        assert "**web_browser**" in summary
        assert "**memory_agent**" in summary
        assert "**memory_shared**" in summary

    def test_agent_specific_still_in_summary(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "### email" in summary
        assert "**email_mine**" in summary


class TestRealBaselineConfig:
    """Tests using the actual config files (config/baseline.yaml)."""

    def test_real_baseline_loads(self):
        baseline = load_baseline()
        assert "web" in baseline
        assert "memory" in baseline
        assert "slack_io" in baseline
        assert "scheduled_tasks" in baseline

    def test_real_lisa_gets_baseline(self):
        caps = get_agent_capabilities("lisa")
        # Agent-specific
        assert "email" in caps.capabilities
        assert "calendar" in caps.capabilities
        # From baseline
        assert "web" in caps.capabilities
        assert "memory" in caps.capabilities
        assert "slack_io" in caps.capabilities
        assert "scheduled_tasks" in caps.capabilities

    def test_real_lisa_memory_scoped(self):
        caps = get_agent_capabilities("lisa")
        memory_instances = caps.capabilities["memory"]
        agent_inst = next(i for i in memory_instances if i.instance == "agent")
        assert agent_inst.account == "lisa"

    def test_real_lisa_total_capability_types(self):
        """Lisa should have 6 capability types: email, calendar + 4 baseline."""
        caps = get_agent_capabilities("lisa")
        assert len(caps.capabilities) == 6
