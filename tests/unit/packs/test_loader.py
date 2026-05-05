"""Unit tests for router.packs.loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from router.packs.loader import Pack, PackError, discover_packs, load_pack

pytestmark = pytest.mark.unit


def _write_pack(packs_dir: Path, name: str, manifest: str, *, files: dict[str, str] | None = None) -> Path:
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(textwrap.dedent(manifest).strip() + "\n")
    for filename, content in (files or {}).items():
        (pack_dir / filename).write_text(content)
    return pack_dir


class TestLoadPack:
    def test_minimal_pack(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(tmp_path, "github", "name: github")
        pack = load_pack(pack_dir)
        assert pack.name == "github"
        assert pack.path == pack_dir
        assert pack.description == ""
        assert pack.needs == []
        assert pack.cli is None
        assert pack.approve == []

    def test_full_pack(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(
            tmp_path,
            "github",
            """
            name: github
            description: GitHub via the gh CLI
            needs: [GITHUB_TOKEN]
            cli: gh
            approve: [merge]
            """,
        )
        pack = load_pack(pack_dir)
        assert pack.description == "GitHub via the gh CLI"
        assert pack.needs == ["GITHUB_TOKEN"]
        assert pack.cli == "gh"
        assert pack.approve == ["merge"]

    def test_companion_files_detected(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(
            tmp_path,
            "github",
            "name: github",
            files={
                "prompt.md": "# Github\n",
                "mcp.json": "{}",
                "authenticate.py": "async def acquire(say): ...\n",
            },
        )
        pack = load_pack(pack_dir)
        assert pack.prompt_path == pack_dir / "prompt.md"
        assert pack.mcp_path == pack_dir / "mcp.json"
        assert pack.authenticate_path == pack_dir / "authenticate.py"

    def test_missing_companion_files_return_none(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(tmp_path, "github", "name: github")
        pack = load_pack(pack_dir)
        assert pack.prompt_path is None
        assert pack.mcp_path is None
        assert pack.authenticate_path is None

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "ghost"
        pack_dir.mkdir()
        with pytest.raises(PackError, match="manifest not found"):
            load_pack(pack_dir)

    def test_name_mismatch_raises(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(tmp_path, "github", "name: gh")
        with pytest.raises(PackError, match="does not match directory name"):
            load_pack(pack_dir)

    def test_missing_name_raises(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(tmp_path, "github", "description: no name")
        with pytest.raises(PackError, match="missing required 'name'"):
            load_pack(pack_dir)

    def test_needs_must_be_list(self, tmp_path: Path) -> None:
        pack_dir = _write_pack(
            tmp_path,
            "bad",
            """
            name: bad
            needs: GITHUB_TOKEN
            """,
        )
        with pytest.raises(PackError, match="'needs' must be a list"):
            load_pack(pack_dir)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "broken"
        pack_dir.mkdir()
        (pack_dir / "pack.yaml").write_text("name: broken\n  : invalid")
        with pytest.raises(PackError, match="failed to parse"):
            load_pack(pack_dir)


class TestDiscoverPacks:
    def test_empty_dir(self, tmp_path: Path) -> None:
        assert discover_packs(tmp_path) == {}

    def test_missing_dir(self, tmp_path: Path) -> None:
        assert discover_packs(tmp_path / "ghost") == {}

    def test_discovers_multiple(self, tmp_path: Path) -> None:
        _write_pack(tmp_path, "github", "name: github")
        _write_pack(tmp_path, "posthog", "name: posthog")
        packs = discover_packs(tmp_path)
        assert set(packs.keys()) == {"github", "posthog"}
        assert isinstance(packs["github"], Pack)

    def test_skips_underscore_dirs(self, tmp_path: Path) -> None:
        _write_pack(tmp_path, "_template", "name: _template")
        _write_pack(tmp_path, "github", "name: github")
        packs = discover_packs(tmp_path)
        assert set(packs.keys()) == {"github"}

    def test_skips_dirs_without_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "lonely").mkdir()
        _write_pack(tmp_path, "github", "name: github")
        packs = discover_packs(tmp_path)
        assert set(packs.keys()) == {"github"}

    def test_malformed_pack_raises(self, tmp_path: Path) -> None:
        _write_pack(tmp_path, "github", "name: nope")
        with pytest.raises(PackError):
            discover_packs(tmp_path)
