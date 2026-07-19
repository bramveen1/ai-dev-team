"""Smoke tests for the brevo pack.

Guard the pack's shape (manifest fields, companion files, exposed coroutine)
plus the Maton-gateway token acquisition flow's happy and error paths. The
real Maton API is mocked via httpx; the live ``GET /v3/account`` round-trip
is exercised manually post-merge per packs/brevo/README.md.
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
BREVO_PACK_DIR = REPO_ROOT / "packs" / "brevo"


def _load_authenticate_module():
    """Import packs/brevo/authenticate.py once for the test session."""
    spec = importlib.util.spec_from_file_location(
        "_test_pack_brevo_authenticate",
        BREVO_PACK_DIR / "authenticate.py",
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
        pack = load_pack(BREVO_PACK_DIR)
        assert pack.name == "brevo"
        assert pack.cli is None
        assert pack.needs == ["BREVO_API_KEY"]
        assert pack.approve == ["send-campaign", "send-bulk"]
        assert pack.description.strip()

    def test_companion_files_exist(self) -> None:
        pack = load_pack(BREVO_PACK_DIR)
        assert pack.prompt_path is not None
        assert pack.authenticate_path is not None

    def test_prompt_mentions_gateway_and_env_var(self) -> None:
        text = (BREVO_PACK_DIR / "prompt.md").read_text()
        assert "BREVO_API_KEY" in text
        assert "gateway.maton.ai/brevo" in text
        # Approval rules are documented for both gated verbs
        assert "send-campaign" in text
        assert "send-bulk" in text
        assert "draft-approval" in text
        # No hardcoded literal token leaked in
        assert "Bearer xkeysib-" not in text

    def test_prompt_documents_bulk_send_rule(self) -> None:
        """The prompt must spell out the 1:1 vs bulk rule for /smtp/email,
        because the manifest can't express it — only the agent can."""
        text = (BREVO_PACK_DIR / "prompt.md").read_text()
        # The single-recipient ungated case must be called out explicitly.
        assert "single `to`" in text or "single recipient" in text
        # The fan-out triggers must be enumerated.
        assert "messageVersions" in text
        assert "list" in text.lower() and "segment" in text.lower()

    def test_authenticate_exposes_acquire_coroutine(self) -> None:
        module = _load_authenticate_module()
        assert inspect.iscoroutinefunction(module.acquire)
        assert list(inspect.signature(module.acquire).parameters.keys()) == ["say"]


# ── Token acquisition flow ───────────────────────────────────────────


def _patch_validate(
    *,
    payload: dict | None = None,
    status: int = 200,
    body: str = "",
) -> object:
    """Mock the Maton ``GET /brevo/v3/account`` endpoint."""
    if payload is None:
        payload = {"email": "ops@example.com", "companyName": "Example Co"}

    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = body or str(payload)

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("httpx.AsyncClient", return_value=ctx)


class TestAcquireBrevoToken:
    @pytest.mark.asyncio
    async def test_happy_path_returns_token(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["xkeysib-abcdef1234567890"])
        with _patch_validate(payload={"email": "ops@example.com", "companyName": "Acme"}):
            secrets = await module.acquire(prompt)
        assert secrets == {"BREVO_API_KEY": "xkeysib-abcdef1234567890"}
        assert any("Token validated" in c for c in prompt.calls)
        assert any("ops@example.com" in c and "Acme" in c for c in prompt.calls)

    @pytest.mark.asyncio
    async def test_email_only_label(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["xkeysib-x"])
        with _patch_validate(payload={"email": "ops@example.com"}):
            await module.acquire(prompt)
        assert any("ops@example.com" in c for c in prompt.calls)

    @pytest.mark.asyncio
    async def test_minimal_payload_falls_back_to_generic_label(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["xkeysib-x"])
        with _patch_validate(payload={"plan": []}):
            await module.acquire(prompt)
        assert any("account reachable" in c for c in prompt.calls)

    @pytest.mark.asyncio
    async def test_strips_whitespace_and_backticks(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["   `xkeysib-xyz`   "])
        with _patch_validate():
            secrets = await module.acquire(prompt)
        assert secrets == {"BREVO_API_KEY": "xkeysib-xyz"}

    @pytest.mark.asyncio
    async def test_empty_reply_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["   "])
        with pytest.raises(RuntimeError, match="no token received"):
            await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_unauthorized_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["xkeysib-bad"])
        with _patch_validate(status=401):
            with pytest.raises(RuntimeError, match="rejected the token"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_other_status_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["xkeysib-xyz"])
        with _patch_validate(status=503, body="upstream down"):
            with pytest.raises(RuntimeError, match="Maton returned 503"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_unexpected_payload_raises(self) -> None:
        module = _load_authenticate_module()
        prompt = _CapturingPrompt(replies=["xkeysib-xyz"])
        with _patch_validate(payload={}):
            with pytest.raises(RuntimeError, match="unexpected payload shape"):
                await module.acquire(prompt)

    @pytest.mark.asyncio
    async def test_rejects_plain_say_callable(self) -> None:
        """Passing a non-InputPrompt callable is a programming error."""
        module = _load_authenticate_module()
        say = AsyncMock()
        with pytest.raises(RuntimeError, match="requires an InputPrompt"):
            await module.acquire(say)
