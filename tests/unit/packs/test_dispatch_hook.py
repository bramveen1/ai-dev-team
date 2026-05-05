"""Unit tests for router.packs.dispatch_hook.

These tests use a tmp_path-based agents dir + packs dir so they don't
depend on the real ``config/agents/*`` or ``packs/*`` layout.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from router.packs.dispatch_hook import (
    CONTAINER_PACKS_DIR,
    pack_cli_extras,
)
from router.packs.secret_store import SecretStore

pytestmark = pytest.mark.unit


def _write_agent_manifest(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).strip() + "\n")
    return path


def _write_pack(packs_dir: Path, name: str, *, manifest: str = None, files: dict[str, str] | None = None) -> Path:
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(textwrap.dedent(manifest or f"name: {name}").strip() + "\n")
    for filename, content in (files or {}).items():
        (pack_dir / filename).write_text(content)
    return pack_dir


class TestPackCliExtrasDormant:
    """When agent.yaml has no ``packs:`` key, extras are empty."""

    def test_no_packs_key_returns_empty(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lisa" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Lisa\ncontainer: lisa")

        extras = pack_cli_extras(
            "lisa",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}

    def test_missing_manifest_returns_empty(self, tmp_path: Path) -> None:
        extras = pack_cli_extras(
            "ghost",
            manifest_path=tmp_path / "ghost.yaml",
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}

    def test_empty_packs_list_returns_empty(self, tmp_path: Path) -> None:
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam\npacks: []")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}


class TestPackCliExtrasWithPacks:
    @pytest.fixture()
    def setup(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()

        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            container: sam
            packs: [github]
            """,
        )

        _write_pack(
            packs_dir,
            "github",
            manifest="""
            name: github
            description: GitHub via gh
            needs: [GITHUB_TOKEN]
            cli: gh
            """,
            files={
                "prompt.md": "# GitHub access\n",
                "mcp.json": json.dumps({"mcpServers": {"gh": {"command": "npx", "args": ["-y", "@gh/mcp"]}}}),
            },
        )
        return agents_dir, packs_dir

    def test_prompt_file_added(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == [f"{CONTAINER_PACKS_DIR}/github/prompt.md"]

    def test_mcp_config_written(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            tmp_dir=tmp_path,
        )
        assert extras.mcp_config_path is not None
        rendered = json.loads(Path(extras.mcp_config_path).read_text())
        assert "gh" in rendered["mcpServers"]
        assert rendered["mcpServers"]["gh"]["command"] == "npx"

    def test_env_injected_from_secret_store(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        store = SecretStore(path=tmp_path / "secrets.json")
        store.set("github", {"GITHUB_TOKEN": "ghp_test"})

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=store,
        )
        assert extras.env == {"GITHUB_TOKEN": "ghp_test"}

    def test_env_case_insensitive_lookup(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        store = SecretStore(path=tmp_path / "secrets.json")
        store.set("github", {"github_token": "ghp_lower"})

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=store,
        )
        assert extras.env == {"GITHUB_TOKEN": "ghp_lower"}

    def test_missing_secret_logged_not_raised(self, setup, tmp_path: Path) -> None:
        """A pack declaring needs but with no stored secrets should not crash."""
        agents_dir, packs_dir = setup
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.env == {}

    def test_unknown_pack_is_skipped(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            packs: [ghost]
            """,
        )
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}


class TestMcpConfigMerging:
    def test_no_mcp_files_yields_no_config(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            packs: [thinpack]
            """,
        )
        _write_pack(packs_dir, "thinpack", manifest="name: thinpack")

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.mcp_config_path is None

    def test_two_packs_servers_merged(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            packs: [a, b]
            """,
        )
        _write_pack(
            packs_dir,
            "a",
            files={"mcp.json": json.dumps({"mcpServers": {"alpha": {"command": "a"}}})},
        )
        _write_pack(
            packs_dir,
            "b",
            files={"mcp.json": json.dumps({"mcpServers": {"beta": {"command": "b"}}})},
        )
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            tmp_dir=tmp_path,
        )
        cfg = json.loads(Path(extras.mcp_config_path).read_text())
        assert set(cfg["mcpServers"].keys()) == {"alpha", "beta"}
