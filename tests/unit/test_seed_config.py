"""Tests for scripts.seed_config — config/ seeding from config.example/ (#378).

`make seed-config` used to be `cp -rn config.example/. config/`, so an
already-seeded host never picked up updated tracked defaults. These tests
exercise the actual copy/sync behavior against a pre-populated config/ dir,
not just a grep of the Makefile.
"""

from __future__ import annotations

import pytest

from scripts.seed_config import seed_config

pytestmark = pytest.mark.unit


@pytest.fixture
def example_dir(tmp_path):
    base = tmp_path / "config.example"
    (base / "shared").mkdir(parents=True)
    (base / "agents" / "sam").mkdir(parents=True)
    (base / "secrets").mkdir(parents=True)

    (base / "shared" / "WORLDVIEW.md").write_text("universal rules v2\n")
    (base / "shared" / "MEMORY.md").write_text("template org memory\n")
    (base / "agents" / "sam" / "role.md").write_text("role v2\n")
    (base / "agents" / "sam" / "agent.yaml").write_text("name: Sam\ncontainer_timeout: 1800\n")
    (base / "secrets" / "gh-aidt-sam.token").write_text("# placeholder\n")
    return base


class TestSeedOnceFiles:
    def test_agent_yaml_never_overwritten_once_present(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "agents" / "sam").mkdir(parents=True)
        (config_dir / "agents" / "sam" / "agent.yaml").write_text("name: Sam\npacks:\n  - github\n")

        seed_config(example_dir, config_dir)

        # Host-mutated packs: list must survive, not get clobbered by the template.
        assert "packs" in (config_dir / "agents" / "sam" / "agent.yaml").read_text()

    def test_agent_yaml_copied_when_missing(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        synced, preserved = seed_config(example_dir, config_dir)

        assert (config_dir / "agents" / "sam" / "agent.yaml").exists()
        assert "agents/sam/agent.yaml" in synced

    def test_secrets_never_overwritten_once_present(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "secrets").mkdir(parents=True)
        (config_dir / "secrets" / "gh-aidt-sam.token").write_text("ghp_realtoken\n")

        seed_config(example_dir, config_dir)

        assert (config_dir / "secrets" / "gh-aidt-sam.token").read_text() == "ghp_realtoken\n"

    def test_shared_memory_never_overwritten_once_present(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "shared").mkdir(parents=True)
        (config_dir / "shared" / "MEMORY.md").write_text("curated host memory\n")

        seed_config(example_dir, config_dir)

        assert (config_dir / "shared" / "MEMORY.md").read_text() == "curated host memory\n"


class TestTrackedDefaultsSync:
    def test_updated_tracked_default_reaches_seeded_host(self, example_dir, tmp_path):
        """The bug in #378: a changed config.example/ file must propagate."""
        config_dir = tmp_path / "config"
        (config_dir / "shared").mkdir(parents=True)
        (config_dir / "shared" / "WORLDVIEW.md").write_text("universal rules v1 (stale)\n")

        synced, _ = seed_config(example_dir, config_dir)

        assert (config_dir / "shared" / "WORLDVIEW.md").read_text() == "universal rules v2\n"
        assert "shared/WORLDVIEW.md" in synced

    def test_role_md_synced_on_change(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "agents" / "sam").mkdir(parents=True)
        (config_dir / "agents" / "sam" / "role.md").write_text("role v1 (stale)\n")

        seed_config(example_dir, config_dir)

        assert (config_dir / "agents" / "sam" / "role.md").read_text() == "role v2\n"

    def test_unchanged_tracked_default_not_reported_as_synced(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        (config_dir / "shared").mkdir(parents=True)
        (config_dir / "shared" / "WORLDVIEW.md").write_text("universal rules v2\n")

        synced, _ = seed_config(example_dir, config_dir)

        assert "shared/WORLDVIEW.md" not in synced

    def test_missing_tracked_default_copied(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        synced, _ = seed_config(example_dir, config_dir)

        assert (config_dir / "shared" / "WORLDVIEW.md").exists()
        assert "shared/WORLDVIEW.md" in synced


class TestFreshHost:
    def test_full_seed_on_empty_config_dir(self, example_dir, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        synced, preserved = seed_config(example_dir, config_dir)

        assert preserved == []
        assert (config_dir / "agents" / "sam" / "role.md").exists()
        assert (config_dir / "secrets" / "gh-aidt-sam.token").exists()
        assert (config_dir / "shared" / "MEMORY.md").exists()
