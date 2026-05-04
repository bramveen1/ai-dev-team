"""Tests for scripts.add_agent — agent wizard."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from scripts.add_agent import (
    AgentSpec,
    append_env,
    load_spec_from_yaml,
    main,
    write_agent_files,
    write_slack_manifest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def maya_spec_yaml(tmp_path) -> Path:
    """A complete --from-yaml input fixture for an agent named 'maya'."""
    path = tmp_path / "maya.yaml"
    path.write_text(
        textwrap.dedent("""\
            id: maya
            name: Maya
            container: maya
            thinking_status: "is sketching…"
            role: |
              # Maya — Designer

              Design lead.
            personality: |
              # Maya — Personality

              Crisp, visual.
            capabilities: {}
            scheduled_tasks: []
            slack:
              bot_token: xoxb-test-bot
              app_token: xapp-test-app
              signing_secret: test-secret
        """)
    )
    return path


@pytest.fixture
def cli_dirs(tmp_path):
    """Provide isolated agents/slack-manifest/env paths so the wizard never touches the real repo."""
    return {
        "agents_dir": tmp_path / "agents",
        "slack_dir": tmp_path / "slack-manifests",
        "env_file": tmp_path / ".env",
    }


def _run(maya_spec: Path, dirs: dict, *extra: str) -> int:
    return main(
        [
            "--from-yaml",
            str(maya_spec),
            "--agents-dir",
            str(dirs["agents_dir"]),
            "--slack-manifest-dir",
            str(dirs["slack_dir"]),
            "--env-file",
            str(dirs["env_file"]),
            "--no-render-compose",
            "--no-validate",
            *extra,
        ]
    )


class TestFromYaml:
    def test_writes_agent_yaml_role_personality(self, maya_spec_yaml, cli_dirs):
        rc = _run(maya_spec_yaml, cli_dirs)
        assert rc == 0

        target = cli_dirs["agents_dir"] / "maya"
        assert (target / "agent.yaml").exists()
        assert (target / "role.md").exists()
        assert (target / "personality.md").exists()

        manifest = yaml.safe_load((target / "agent.yaml").read_text())
        assert manifest["name"] == "Maya"
        assert manifest["container"] == "maya"
        assert manifest["thinking_status"] == "is sketching…"

        assert "Maya — Designer" in (target / "role.md").read_text()
        assert "Crisp, visual" in (target / "personality.md").read_text()

    def test_writes_slack_manifest(self, maya_spec_yaml, cli_dirs):
        _run(maya_spec_yaml, cli_dirs)

        manifest_path = cli_dirs["slack_dir"] / "maya.yaml"
        assert manifest_path.exists()

        slack = yaml.safe_load(manifest_path.read_text())
        assert slack["display_information"]["name"] == "Maya"
        assert slack["features"]["bot_user"]["display_name"] == "Maya"

        commands = slack["features"]["slash_commands"]
        assert commands[0]["command"] == "/maya-tasks"

    def test_slack_manifest_with_slash_prefix(self, maya_spec_yaml, cli_dirs):
        _run(maya_spec_yaml, cli_dirs, "--slash-prefix", "dev-")

        manifest = yaml.safe_load((cli_dirs["slack_dir"] / "maya.yaml").read_text())
        assert manifest["features"]["slash_commands"][0]["command"] == "/dev-maya-tasks"

    def test_appends_real_tokens_to_env(self, maya_spec_yaml, cli_dirs):
        _run(maya_spec_yaml, cli_dirs)

        env = cli_dirs["env_file"].read_text()
        assert "MAYA_BOT_TOKEN=xoxb-test-bot" in env
        assert "MAYA_APP_TOKEN=xapp-test-app" in env
        assert "MAYA_SIGNING_SECRET=test-secret" in env

    def test_no_slack_writes_placeholders(self, maya_spec_yaml, cli_dirs):
        _run(maya_spec_yaml, cli_dirs, "--no-slack")

        env = cli_dirs["env_file"].read_text()
        assert "MAYA_BOT_TOKEN=xoxb-..." in env
        assert "xoxb-test-bot" not in env

    def test_refuses_to_overwrite_existing_agent_dir(self, maya_spec_yaml, cli_dirs):
        (cli_dirs["agents_dir"] / "maya").mkdir(parents=True)

        rc = _run(maya_spec_yaml, cli_dirs)
        assert rc == 1


class TestLoadSpecFromYaml:
    def test_parses_full_spec(self, maya_spec_yaml):
        spec = load_spec_from_yaml(maya_spec_yaml, no_slack=False)
        assert spec.id == "maya"
        assert spec.name == "Maya"
        assert spec.bot_token == "xoxb-test-bot"

    def test_no_slack_drops_tokens(self, maya_spec_yaml):
        spec = load_spec_from_yaml(maya_spec_yaml, no_slack=True)
        assert spec.bot_token is None
        assert spec.app_token is None
        assert spec.signing_secret is None

    def test_invalid_id_rejected(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("id: 'BAD NAME'\nname: Bad\n")
        with pytest.raises(ValueError, match="invalid agent id"):
            load_spec_from_yaml(path, no_slack=True)

    def test_id_inferred_from_filename(self, tmp_path):
        path = tmp_path / "lin.yaml"
        path.write_text("name: Lin\n")
        spec = load_spec_from_yaml(path, no_slack=True)
        assert spec.id == "lin"

    def test_missing_optional_fields_get_templates(self, tmp_path):
        path = tmp_path / "lin.yaml"
        path.write_text("name: Lin\n")
        spec = load_spec_from_yaml(path, no_slack=True)
        assert "Lin" in spec.role
        assert "Personality" in spec.personality


class TestAppendEnv:
    def test_creates_env_if_missing(self, tmp_path):
        env = tmp_path / ".env"
        spec = AgentSpec(
            id="maya",
            name="Maya",
            container="maya",
            thinking_status="…",
            role="",
            personality="",
            bot_token="xoxb-x",
            app_token="xapp-x",
            signing_secret="sec",
        )
        assert append_env(spec, env)
        text = env.read_text()
        assert "MAYA_BOT_TOKEN=xoxb-x" in text

    def test_skips_if_agent_already_present(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("MAYA_BOT_TOKEN=existing\n")
        spec = AgentSpec(
            id="maya",
            name="Maya",
            container="maya",
            thinking_status="…",
            role="",
            personality="",
            bot_token="xoxb-new",
        )
        assert not append_env(spec, env)
        # existing value preserved
        assert "existing" in env.read_text()
        assert "xoxb-new" not in env.read_text()

    def test_writes_placeholders_when_tokens_missing(self, tmp_path):
        env = tmp_path / ".env"
        spec = AgentSpec(
            id="maya",
            name="Maya",
            container="maya",
            thinking_status="…",
            role="",
            personality="",
        )
        append_env(spec, env)
        assert "MAYA_BOT_TOKEN=xoxb-..." in env.read_text()


class TestWriteAgentFiles:
    def test_drops_empty_scheduled_tasks_from_manifest(self, tmp_path):
        spec = AgentSpec(
            id="maya",
            name="Maya",
            container="maya",
            thinking_status="…",
            role="role",
            personality="personality",
        )
        write_agent_files(spec, tmp_path)
        manifest = yaml.safe_load((tmp_path / "maya" / "agent.yaml").read_text())
        assert "scheduled_tasks" not in manifest

    def test_includes_scheduled_tasks_when_present(self, tmp_path):
        spec = AgentSpec(
            id="maya",
            name="Maya",
            container="maya",
            thinking_status="…",
            role="role",
            personality="personality",
            scheduled_tasks=[{"name": "Daily check", "prompt": "x", "schedule_cron": "0 9 * * *", "enabled": False}],
        )
        write_agent_files(spec, tmp_path)
        manifest = yaml.safe_load((tmp_path / "maya" / "agent.yaml").read_text())
        assert manifest["scheduled_tasks"][0]["name"] == "Daily check"


class TestWriteSlackManifest:
    def test_includes_required_oauth_scopes(self, tmp_path):
        spec = AgentSpec(
            id="maya",
            name="Maya",
            container="maya",
            thinking_status="…",
            role="",
            personality="",
        )
        path = write_slack_manifest(spec, tmp_path)
        manifest = yaml.safe_load(path.read_text())
        scopes = manifest["oauth_config"]["scopes"]["bot"]
        for required in ("app_mentions:read", "chat:write", "commands", "assistant:write"):
            assert required in scopes


class TestRouterDiscoversNewAgent:
    """The point of the wizard: drop a manifest in, router picks it up next boot."""

    def test_discover_agents_picks_up_new_dir(self, maya_spec_yaml, cli_dirs):
        _run(maya_spec_yaml, cli_dirs)

        # The router's discover_agents() takes an explicit agents_dir, so we
        # can test against the temp dir we just wrote into.
        from router.config import discover_agents

        agents = discover_agents(cli_dirs["agents_dir"])
        assert "maya" in agents
        assert agents["maya"]["name"] == "Maya"
        assert agents["maya"]["container"] == "maya"
        assert agents["maya"]["thinking_status"] == "is sketching…"
