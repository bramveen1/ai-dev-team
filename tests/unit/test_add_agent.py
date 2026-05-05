"""Tests for scripts.add_agent — agent wizard."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from scripts.add_agent import (
    AgentSpec,
    _discover_pack_names,
    _prompt_packs,
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
        assert spec.packs == []

    def test_parses_packs_list(self, tmp_path):
        path = tmp_path / "sam.yaml"
        path.write_text("name: Sam\npacks: [github, posthog]\n")
        spec = load_spec_from_yaml(path, no_slack=True)
        assert spec.packs == ["github", "posthog"]

    def test_packs_must_be_a_list(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("name: Bad\npacks: github\n")
        with pytest.raises(ValueError, match="'packs' must be a list"):
            load_spec_from_yaml(path, no_slack=True)

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

    def test_writes_packs_field(self, tmp_path):
        spec = AgentSpec(
            id="sam",
            name="Sam",
            container="sam",
            thinking_status="…",
            role="role",
            personality="personality",
            packs=["github"],
        )
        write_agent_files(spec, tmp_path)
        manifest = yaml.safe_load((tmp_path / "sam" / "agent.yaml").read_text())
        assert manifest["packs"] == ["github"]

    def test_writes_empty_packs_list_when_none_chosen(self, tmp_path):
        spec = AgentSpec(
            id="alex",
            name="Alex",
            container="alex",
            thinking_status="…",
            role="role",
            personality="personality",
        )
        write_agent_files(spec, tmp_path)
        manifest = yaml.safe_load((tmp_path / "alex" / "agent.yaml").read_text())
        assert manifest["packs"] == []

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


class TestDiscoverPackNames:
    def test_returns_sorted_pack_dirs(self, tmp_path):
        for name in ("zoho-mail", "github", "_template"):
            pack_dir = tmp_path / name
            pack_dir.mkdir()
            (pack_dir / "pack.yaml").write_text(f"name: {name}\n")
        # A directory without pack.yaml is ignored.
        (tmp_path / "stale").mkdir()

        assert _discover_pack_names(tmp_path) == ["github", "zoho-mail"]

    def test_missing_packs_dir_returns_empty(self, tmp_path):
        assert _discover_pack_names(tmp_path / "nonexistent") == []


class TestPromptPacks:
    def _packs_dir(self, tmp_path, names):
        for name in names:
            d = tmp_path / name
            d.mkdir()
            (d / "pack.yaml").write_text(f"name: {name}\n")
        return tmp_path

    def test_resolves_indices(self, tmp_path, monkeypatch):
        packs_dir = self._packs_dir(tmp_path, ["github", "posthog", "zoho-mail"])
        monkeypatch.setattr("builtins.input", lambda _: "1, 3")
        chosen = _prompt_packs(packs_dir)
        assert chosen == ["github", "zoho-mail"]

    def test_resolves_names_and_dedupes(self, tmp_path, monkeypatch):
        packs_dir = self._packs_dir(tmp_path, ["github", "posthog"])
        monkeypatch.setattr("builtins.input", lambda _: "github, github, posthog")
        chosen = _prompt_packs(packs_dir)
        assert chosen == ["github", "posthog"]

    def test_blank_returns_empty_list(self, tmp_path, monkeypatch):
        packs_dir = self._packs_dir(tmp_path, ["github"])
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _prompt_packs(packs_dir) == []

    def test_no_packs_dir_returns_empty(self, tmp_path):
        # No prompt is issued when there are zero packs to pick from.
        assert _prompt_packs(tmp_path / "nonexistent") == []

    def test_unknown_pack_is_ignored(self, tmp_path, monkeypatch):
        packs_dir = self._packs_dir(tmp_path, ["github"])
        monkeypatch.setattr("builtins.input", lambda _: "github, made-up-pack, 42")
        assert _prompt_packs(packs_dir) == ["github"]


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
