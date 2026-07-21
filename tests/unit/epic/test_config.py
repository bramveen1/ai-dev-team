"""Unit tests for router.epic.config — epic orchestrator loop config (#755)."""

from __future__ import annotations

import pytest
import yaml

from router.epic.config import load_epic_config

pytestmark = pytest.mark.unit


class TestLoadEpicConfig:
    def test_defaults_when_no_file(self, tmp_path):
        cfg = load_epic_config(str(tmp_path / "does_not_exist.yaml"))
        assert cfg == {
            "repo": "",
            "base_branch": "main",
            "epics": [],
            "worker_agent": None,
            "worker_model": "sonnet",
            "worker_persona": "dev",
            "worker_budget_seconds": 1800,
        }

    def test_reads_repo_and_epics_from_file(self, tmp_path):
        p = tmp_path / "epic.yaml"
        p.write_text(
            yaml.dump(
                {
                    "epic_orchestrator": {
                        "repo": "org/repo",
                        "epics": [{"number": 751, "slug": "auto-feature-orchestrator"}],
                    }
                }
            )
        )
        cfg = load_epic_config(str(p))
        assert cfg["repo"] == "org/repo"
        assert cfg["epics"] == [{"number": 751, "slug": "auto-feature-orchestrator"}]
        # Unset keys still fall back to defaults.
        assert cfg["base_branch"] == "main"
        assert cfg["worker_model"] == "sonnet"

    def test_no_epic_orchestrator_block_uses_defaults(self, tmp_path):
        p = tmp_path / "epic.yaml"
        p.write_text(yaml.dump({"some_other_key": True}))
        cfg = load_epic_config(str(p))
        assert cfg["repo"] == ""
        assert cfg["epics"] == []

    def test_invalid_yaml_returns_defaults(self, tmp_path):
        p = tmp_path / "epic.yaml"
        p.write_text("epic_orchestrator: [unterminated")
        cfg = load_epic_config(str(p))
        assert cfg["repo"] == ""
        assert cfg["epics"] == []

    def test_default_path_resolves_to_config_epic_yaml(self):
        # No config_path override — must not raise even if config/epic.yaml
        # is missing or empty in this environment.
        cfg = load_epic_config()
        assert "repo" in cfg
        assert "epics" in cfg
