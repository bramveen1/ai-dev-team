"""Unit tests for router/agent_scaffold.py — shared add-agent primitives."""

from __future__ import annotations

import pytest
import yaml

from router.agent_scaffold import (
    NAME_RE,
    AgentSpec,
    personality_template,
    role_template,
    slack_manifest_dict,
    slack_manifest_yaml,
    write_agent_files,
    write_slack_manifest,
)

pytestmark = pytest.mark.unit


def _spec(**overrides) -> AgentSpec:
    base = dict(
        id="nina",
        name="Nina",
        container="nina",
        thinking_status="is pondering…",
        role=role_template("Nina", "Test agent."),
        personality=personality_template("Nina", "Cheerful."),
        packs=["github"],
    )
    base.update(overrides)
    return AgentSpec(**base)


class TestNameRe:
    def test_valid_ids(self):
        for good in ("nina", "a", "agent-2", "x_y"):
            assert NAME_RE.match(good), good

    def test_rejects_traversal_and_uppercase(self):
        for bad in ("../etc", "Nina", "2nina", "ni na", "ni/na", ".hidden", ""):
            assert not NAME_RE.match(bad), bad


class TestWriteAgentFiles:
    def test_writes_manifest_role_personality(self, tmp_path):
        paths = write_agent_files(_spec(), tmp_path)
        assert [p.name for p in paths] == ["agent.yaml", "role.md", "personality.md"]
        manifest = yaml.safe_load((tmp_path / "nina" / "agent.yaml").read_text())
        assert manifest == {
            "name": "Nina",
            "container": "nina",
            "thinking_status": "is pondering…",
            "packs": ["github"],
        }
        assert "# Nina" in (tmp_path / "nina" / "role.md").read_text()

    def test_existing_dir_raises(self, tmp_path):
        (tmp_path / "nina").mkdir()
        with pytest.raises(FileExistsError):
            write_agent_files(_spec(), tmp_path)

    def test_scheduled_tasks_included_when_present(self, tmp_path):
        tasks = [{"name": "t", "prompt": "p", "schedule_cron": "0 3 * * *", "enabled": True}]
        write_agent_files(_spec(scheduled_tasks=tasks), tmp_path)
        manifest = yaml.safe_load((tmp_path / "nina" / "agent.yaml").read_text())
        assert manifest["scheduled_tasks"] == tasks


class TestSlackManifest:
    def test_dict_shape_and_slash_prefix(self):
        m = slack_manifest_dict(_spec(), slash_prefix="dev-")
        assert m["display_information"]["name"] == "Nina"
        assert m["features"]["slash_commands"][0]["command"] == "/dev-nina-tasks"
        assert m["settings"]["socket_mode_enabled"] is True

    def test_yaml_roundtrips(self):
        text = slack_manifest_yaml(_spec())
        assert yaml.safe_load(text)["features"]["bot_user"]["display_name"] == "Nina"

    def test_write_creates_file(self, tmp_path):
        path = write_slack_manifest(_spec(), tmp_path / "manifests")
        assert path == tmp_path / "manifests" / "nina.yaml"
        assert yaml.safe_load(path.read_text())["display_information"]["name"] == "Nina"
