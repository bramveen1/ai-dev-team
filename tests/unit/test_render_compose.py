"""Tests for scripts.render_compose — docker-compose.yml generator."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from scripts.render_compose import build_compose, main, render

pytestmark = pytest.mark.unit


@pytest.fixture
def agents_dir(tmp_path) -> Path:
    """Build a config/agents-style directory with two agents."""
    base = tmp_path / "agents"
    (base / "lisa").mkdir(parents=True)
    (base / "sam").mkdir(parents=True)

    (base / "lisa" / "agent.yaml").write_text(
        textwrap.dedent("""\
            name: Lisa
            container: lisa
            thinking_status: "is reviewing findings…"
            capabilities: {}
        """)
    )
    (base / "sam" / "agent.yaml").write_text(
        textwrap.dedent("""\
            name: Sam
            container: sam
            thinking_status: "is digging in…"
            capabilities: {}
        """)
    )
    return base


class TestBuildCompose:
    def test_includes_router_and_each_agent(self, agents_dir):
        from router.config import discover_agents

        agents = discover_agents(agents_dir)
        compose = build_compose(agents, agents_dir)

        assert set(compose["services"].keys()) == {"router", "lisa", "sam"}

    def test_volume_names_preserved(self, agents_dir):
        """Volume names must match the legacy `<name>-claude-config` pattern.

        Renaming these would orphan existing Docker volumes and force a
        Claude re-auth. The shared ``agent-tools`` volume (added in PR 2 for
        on-demand CLI installs) is also expected.
        """
        from router.config import discover_agents

        compose = build_compose(discover_agents(agents_dir), agents_dir)

        assert set(compose["volumes"].keys()) == {
            "lisa-claude-config",
            "sam-claude-config",
            "agent-tools",
        }

        for agent in ("lisa", "sam"):
            volumes = compose["services"][agent]["volumes"]
            assert f"{agent}-claude-config:/home/claude/.claude" in volumes
            assert "agent-tools:/opt/tools" in volumes

    def test_router_env_includes_token_trio_per_agent(self, agents_dir):
        from router.config import discover_agents

        compose = build_compose(discover_agents(agents_dir), agents_dir)
        env = compose["services"]["router"]["environment"]

        for agent in ("LISA", "SAM"):
            assert f"{agent}_BOT_TOKEN=${{{agent}_BOT_TOKEN}}" in env
            assert f"{agent}_APP_TOKEN=${{{agent}_APP_TOKEN}}" in env
            assert f"{agent}_SIGNING_SECRET=${{{agent}_SIGNING_SECRET}}" in env

        assert any(line.startswith("SESSION_TIMEOUT=") for line in env)
        assert any(line.startswith("LOG_LEVEL=") for line in env)

    def test_router_depends_on_each_agent(self, agents_dir):
        from router.config import discover_agents

        compose = build_compose(discover_agents(agents_dir), agents_dir)
        assert sorted(compose["services"]["router"]["depends_on"]) == ["lisa", "sam"]

    def test_default_agent_uses_shared_base_image(self, agents_dir):
        """No per-agent Dockerfile → use base image with sleep override."""
        from router.config import discover_agents

        compose = build_compose(discover_agents(agents_dir), agents_dir)

        for agent in ("lisa", "sam"):
            svc = compose["services"][agent]
            assert svc["image"] == "ai-dev-team-base:latest"
            assert svc["command"] == ["sleep", "infinity"]
            assert "build" not in svc

    def test_per_agent_dockerfile_override(self, agents_dir):
        """A Dockerfile next to agent.yaml switches the service to build mode."""
        from router.config import discover_agents

        (agents_dir / "lisa" / "Dockerfile").write_text("FROM ai-dev-team-base:latest\nCMD sleep infinity\n")

        compose = build_compose(discover_agents(agents_dir), agents_dir)
        lisa = compose["services"]["lisa"]

        assert "image" not in lisa
        assert "build" in lisa
        assert lisa["build"]["context"] == "."
        assert lisa["build"]["dockerfile"].endswith("/Dockerfile")

    def test_container_name_uses_manifest_override(self, tmp_path):
        """When agent.yaml sets container, that wins over the directory name."""
        from router.config import discover_agents

        base = tmp_path / "agents"
        (base / "maya").mkdir(parents=True)
        (base / "maya" / "agent.yaml").write_text("name: Maya\ncontainer: maya-prod\ncapabilities: {}\n")

        compose = build_compose(discover_agents(base), base)
        assert compose["services"]["maya"]["container_name"] == "maya-prod"


class TestRender:
    def test_render_includes_header(self, agents_dir):
        rendered = render(agents_dir)
        assert "GENERATED" in rendered
        assert "docker-compose.override.yml" in rendered

    def test_render_is_valid_yaml(self, agents_dir):
        rendered = render(agents_dir)
        loaded = yaml.safe_load(rendered)
        assert "services" in loaded
        assert "volumes" in loaded


class TestMain:
    def test_writes_to_output_path(self, agents_dir, tmp_path):
        output = tmp_path / "out.yml"
        rc = main(["--agents-dir", str(agents_dir), "-o", str(output)])
        assert rc == 0
        assert output.exists()
        loaded = yaml.safe_load(output.read_text())
        assert "lisa" in loaded["services"]

    def test_check_passes_when_up_to_date(self, agents_dir, tmp_path, capsys):
        output = tmp_path / "out.yml"
        main(["--agents-dir", str(agents_dir), "-o", str(output)])

        rc = main(["--agents-dir", str(agents_dir), "-o", str(output), "--check"])
        assert rc == 0

    def test_check_fails_when_stale(self, agents_dir, tmp_path):
        output = tmp_path / "out.yml"
        output.write_text("services: {}\n")

        rc = main(["--agents-dir", str(agents_dir), "-o", str(output), "--check"])
        assert rc == 1


class TestRealConfig:
    def test_renders_against_real_config(self):
        """The committed config/agents/ should render without error."""
        rendered = render()
        loaded = yaml.safe_load(rendered)
        assert "router" in loaded["services"]
        assert "lisa" in loaded["services"]
        # Volume name preservation — the production volumes hold Claude auth state.
        assert "lisa-claude-config" in loaded["volumes"]
