"""Smoke tests for the zoho-mail pack.

Guard the pack's shape (manifest fields, companion files, exposed coroutine)
plus the Maton-gateway token acquisition flow's happy and error paths. The
real Maton API is mocked via httpx; the live ``GET /api/accounts`` round-trip
is exercised manually post-merge per packs/zoho-mail/README.md.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.packs.grants import InputPrompt
from router.packs.loader import load_pack

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
ZOHO_PACK_DIR = REPO_ROOT / "packs" / "zoho-mail"


def _load_authenticate_module():
    """Import packs/zoho-mail/authenticate.py once for the test session."""
    spec = importlib.util.spec_from_file_location(
        "_test_pack_zoho_mail_authenticate",
        ZOHO_PACK_DIR / "authenticate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _CapturingPrompt(InputPrompt):
    """An InputPrompt where ``prompt`` returns a canned reply and ``__call__``
    captures messages without hitting Slack."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(say=AsyncMock())
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
        pack = load_pack(ZOHO_PACK_DIR)
        assert pack.name == "zoho-mail"
        assert pack.cli is None
        assert pack.needs == ["ZOHO_API_KEY"]
        assert pack.approve == []
        assert pack.description.strip()

    def test_companion_files_exist(self) -> None:
        pack = load_pack(ZOHO_PACK_DIR)
        assert pack.prompt_path is not None
        assert pack.authenticate_path is not None

    def test_prompt_mentions_gateway_and_env_var(self) -> None:
        text = (ZOHO_PACK_DIR / "prompt.md").read_text()
        assert "ZOHO_API_KEY" in text
        assert "gateway.maton.ai" in text
        # No hardcoded literal token leaked in
        assert "Bearer 0jY" not in text

    def test_authenticate_exposes_acquire_coroutine(self) -> None:
        module = _load_authenticate_module()
        assert inspect.iscoroutinefunction(module.acquire)
        assert list(inspect.signature(module.acquire).parameters.keys()) == ["say"]


# ── Token acquisition flow ───────────────────────────────────────────


def _patch_validate(
    *,
    accounts: list | None = None,
    status: int = 200,
    body: str = "",
    payload_shape: str = "data",
) -> object:
    """Mock the Maton accounts endpoint.

    ``payload_shape`` controls how the mocked response wraps the accounts
    list — covering Maton/Zoho's slight response variations:
    ``"data"`` → ``{"data": [...]}`` (Zoho's standard wrapper)
    ``"accounts"`` → ``{"accounts": [...]}``
    ``"bare"`` → ``[...]`` (raw list)
    ``"unexpected"`` → ``{}`` (neither — should raise)
    """
    if accounts is None:
        accounts = [{"accountId": "8329259000000002002"}]

    response = MagicMock()
    response.status_code = status

    if payload_shape == "data":
        response.json.return_value = {"data": accounts}
    elif payload_shape == "accounts":
        response.json.return_value = {"accounts": accounts}
    elif payload_shape == "bare":
        response.json.return_value = accounts
    else:
        response.json.return_value = {}

    response.text = body or str(response.json.return_value)

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("httpx.AsyncClient", return_value=ctx)


class TestAcquireZohoToken:
    @pytest.mark.asyncio
    async def test_happy_path_returns_token(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_abcdef1234567890"])
        with _patch_validate(accounts=[{"accountId": "A1"}, {"accountId": "A2"}]):
            secrets = await module.acquire(prompt)
        assert secrets == {"ZOHO_API_KEY": "maton_abcdef1234567890"}
        assert any("Token validated" in c for c in prompt.calls)
        assert any("2 accounts" in c for c in prompt.calls)

    @pytest.mark.asyncio
    async def test_singular_account_phrasing(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_x"])
        with _patch_validate(accounts=[{"accountId": "A1"}]):
            await module.acquire(prompt)
        assert any("1 account" in c and "1 accounts" not in c for c in prompt.calls)

    @pytest.mark.asyncio
    async def test_strips_whitespace_and_backticks(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["   `maton_xyz`   "])
        with _patch_validate():
            secrets = await module.acquire(prompt)
        assert secrets == {"ZOHO_API_KEY": "maton_xyz"}

    @pytest.mark.asyncio
    async def test_empty_reply_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["   "])
        with pytest.raises(RuntimeError, match="no token received"):
            await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_unauthorized_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_bad"])
        with _patch_validate(status=401):
            with pytest.raises(RuntimeError, match="rejected the token"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_other_status_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_xyz"])
        with _patch_validate(status=503, body="upstream down"):
            with pytest.raises(RuntimeError, match="Maton returned 503"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_unexpected_payload_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_xyz"])
        with _patch_validate(payload_shape="unexpected"):
            with pytest.raises(RuntimeError, match="unexpected payload shape"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_zero_accounts_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_xyz"])
        with _patch_validate(accounts=[]):
            with pytest.raises(RuntimeError, match="zero accounts"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_accepts_bare_list_payload(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_xyz"])
        with _patch_validate(accounts=[{"accountId": "A"}], payload_shape="bare"):
            secrets = await module.acquire(prompt)
        assert secrets == {"ZOHO_API_KEY": "maton_xyz"}

    @pytest.mark.asyncio
    async def test_accepts_accounts_key_payload(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["maton_xyz"])
        with _patch_validate(accounts=[{"accountId": "A"}], payload_shape="accounts"):
            secrets = await module.acquire(prompt)
        assert secrets == {"ZOHO_API_KEY": "maton_xyz"}

    @pytest.mark.asyncio
    async def test_rejects_plain_say_callable(self) -> None:
        """Passing a non-InputPrompt callable is a programming error."""
        module = _load_authenticate_module()
        say = AsyncMock()
        with pytest.raises(RuntimeError, match="requires an InputPrompt"):
            await module.acquire(say)
