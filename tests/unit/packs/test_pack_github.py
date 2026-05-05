"""Smoke tests for the github pack.

Guard the pack's shape (manifest fields, companion files, exposed coroutine)
plus the PAT acquisition flow's happy and error paths. The real GitHub API
is mocked via httpx; the live ``GET /user`` round-trip is exercised manually
post-merge per packs/github/README.md.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.packs.grants import SlackPrompt
from router.packs.loader import load_pack

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB_PACK_DIR = REPO_ROOT / "packs" / "github"


def _load_authenticate_module():
    """Import packs/github/authenticate.py once for the test session."""
    spec = importlib.util.spec_from_file_location(
        "_test_pack_github_authenticate",
        GITHUB_PACK_DIR / "authenticate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _CapturingPrompt(SlackPrompt):
    """A SlackPrompt where ``prompt`` returns a canned reply and ``__call__``
    captures messages without hitting Slack."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(say=AsyncMock(), channel="C1", thread_ts="t.1")
        self._replies = list(replies)
        self.calls: list[str] = []

    async def __call__(self, text: str) -> None:  # type: ignore[override]
        self.calls.append(text)

    async def prompt(self, text: str, *, timeout: float = 600) -> str:  # type: ignore[override]
        self.calls.append(text)
        return self._replies.pop(0)


# ── Pack-shape guards ────────────────────────────────────────────────


class TestPackShape:
    def test_manifest_loads_cleanly(self) -> None:
        pack = load_pack(GITHUB_PACK_DIR)
        assert pack.name == "github"
        assert pack.cli == "gh"
        assert pack.needs == ["GITHUB_TOKEN"]
        assert pack.approve == ["merge"]
        assert pack.description.strip()

    def test_companion_files_exist(self) -> None:
        pack = load_pack(GITHUB_PACK_DIR)
        assert pack.prompt_path is not None
        assert pack.authenticate_path is not None

    def test_prompt_mentions_gh_cli_and_approval(self) -> None:
        text = (GITHUB_PACK_DIR / "prompt.md").read_text()
        assert "gh" in text
        assert "GITHUB_TOKEN" in text
        assert "merge" in text.lower()

    def test_authenticate_exposes_acquire_coroutine(self) -> None:
        module = _load_authenticate_module()
        assert inspect.iscoroutinefunction(module.acquire)
        assert list(inspect.signature(module.acquire).parameters.keys()) == ["say"]

    def test_install_script_exists_and_executable(self) -> None:
        install_sh = GITHUB_PACK_DIR / "install.sh"
        assert install_sh.exists()
        assert install_sh.stat().st_mode & 0o100


# ── PAT acquisition flow ─────────────────────────────────────────────


def _patch_validate(login: str | None = "octocat", status: int = 200, body: str = "") -> object:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"login": login} if login is not None else {}
    response.text = body or (str(response.json.return_value))

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("httpx.AsyncClient", return_value=ctx)


class TestAcquirePAT:
    @pytest.mark.asyncio
    async def test_happy_path_returns_token(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["ghp_abcdef1234567890"])
        with _patch_validate(login="bramveen1"):
            secrets = await module.acquire(prompt)
        assert secrets == {"GITHUB_TOKEN": "ghp_abcdef1234567890"}
        assert any("Token validated" in c for c in prompt.calls)
        assert any("bramveen1" in c for c in prompt.calls)

    @pytest.mark.asyncio
    async def test_strips_whitespace_and_backticks(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["   `ghp_xyz`   "])
        with _patch_validate():
            secrets = await module.acquire(prompt)
        assert secrets == {"GITHUB_TOKEN": "ghp_xyz"}

    @pytest.mark.asyncio
    async def test_empty_reply_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["   "])
        with pytest.raises(RuntimeError, match="no token received"):
            await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_unauthorized_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["ghp_invalid"])
        with _patch_validate(status=401):
            with pytest.raises(RuntimeError, match="rejected the token"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_other_status_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["ghp_xyz"])
        with _patch_validate(status=500, body="boom"):
            with pytest.raises(RuntimeError, match="GitHub returned 500"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_missing_login_in_response_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["ghp_xyz"])
        with _patch_validate(login=None):
            with pytest.raises(RuntimeError, match="no login field"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_rejects_plain_say_callable(self) -> None:
        """Passing a non-SlackPrompt callable is a programming error."""
        module = _load_authenticate_module()
        say = AsyncMock()
        with pytest.raises(RuntimeError, match="requires a SlackPrompt"):
            await module.acquire(say)
