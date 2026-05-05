"""Smoke tests for the github pack — its manifest parses, prompt and
authenticate.py exist, and the authenticate module exposes ``acquire``.

Real device-code flow is exercised against the live GitHub endpoint
manually (see packs/github/README.md). These tests just guard the pack's
shape so PR 4 can't ship a malformed pack.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from router.packs.loader import load_pack

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB_PACK_DIR = REPO_ROOT / "packs" / "github"


def test_manifest_loads_cleanly() -> None:
    pack = load_pack(GITHUB_PACK_DIR)
    assert pack.name == "github"
    assert pack.cli == "gh"
    assert pack.needs == ["GITHUB_TOKEN"]
    assert pack.approve == ["merge"]
    assert pack.description.strip()


def test_companion_files_exist() -> None:
    pack = load_pack(GITHUB_PACK_DIR)
    assert pack.prompt_path is not None
    assert pack.authenticate_path is not None


def test_prompt_mentions_gh_cli_and_approval() -> None:
    """The prompt should tell the agent how to use gh and that merge is gated."""
    text = (GITHUB_PACK_DIR / "prompt.md").read_text()
    assert "gh" in text
    assert "GITHUB_TOKEN" in text
    assert "merge" in text.lower()


def test_authenticate_exposes_acquire_coroutine() -> None:
    """authenticate.py must define ``async def acquire(say)``."""
    spec = importlib.util.spec_from_file_location(
        "_test_pack_github_authenticate",
        GITHUB_PACK_DIR / "authenticate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "acquire")
    assert inspect.iscoroutinefunction(module.acquire)
    sig = inspect.signature(module.acquire)
    assert list(sig.parameters.keys()) == ["say"]


def test_install_script_exists_and_executable() -> None:
    install_sh = GITHUB_PACK_DIR / "install.sh"
    assert install_sh.exists()
    # Owner-execute bit set
    assert install_sh.stat().st_mode & 0o100
