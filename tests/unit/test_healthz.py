"""Unit tests for router.healthz — the /healthz endpoint consumed by the
pull-based deploy daemon (issue #107)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from router import healthz

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_readiness():
    """Each test starts with readiness flipped off — readiness is global state."""
    healthz.reset_ready_for_tests()
    yield
    healthz.reset_ready_for_tests()


@pytest.fixture
def _slack_token_set(monkeypatch):
    """Provide a non-empty Slack bot token so the env-presence check passes."""
    monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)


@pytest.fixture
def _no_slack_token(monkeypatch):
    for name in ("LISA_BOT_TOKEN", "SAM_BOT_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


async def _get_healthz(app):
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        body = await resp.json()
        return resp.status, body


@pytest.mark.asyncio
async def test_healthz_returns_503_before_mark_ready(_slack_token_set):
    """Until mark_ready() is called the probe must fail — guards against
    probing a partially-initialized router during the deploy settle window."""
    status, body = await _get_healthz(healthz.build_app())
    assert status == 503
    assert body == {"status": "not ready"}


@pytest.mark.asyncio
async def test_healthz_returns_200_when_ready_and_token_present(_slack_token_set):
    healthz.mark_ready()
    status, body = await _get_healthz(healthz.build_app())
    assert status == 200
    assert body == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_returns_503_when_ready_but_token_missing(_no_slack_token):
    """A wiped env file shouldn't get marked healthy just because the
    process happens to be running."""
    healthz.mark_ready()
    status, body = await _get_healthz(healthz.build_app())
    assert status == 503
    assert body == {"status": "slack token missing"}


def test_slack_token_present_accepts_any_known_var():
    assert healthz._slack_token_present({"LISA_BOT_TOKEN": "xoxb-x"})
    assert healthz._slack_token_present({"SAM_BOT_TOKEN": "xoxb-x"})
    assert healthz._slack_token_present({"SLACK_BOT_TOKEN": "xoxb-x"})


def test_slack_token_present_rejects_empty_and_unset():
    assert not healthz._slack_token_present({})
    assert not healthz._slack_token_present({"LISA_BOT_TOKEN": ""})
    assert not healthz._slack_token_present({"UNRELATED": "xoxb-x"})


@pytest.mark.asyncio
async def test_start_server_binds_and_serves(monkeypatch):
    """End-to-end: start_server() must actually bind a TCP socket and serve."""
    monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test")
    healthz.mark_ready()
    runner = await healthz.start_server(port=0)  # 0 = let OS pick a free port
    try:
        # AppRunner exposes .sites for the bound TCPSite — port lives there.
        site = next(iter(runner.sites))
        port = site._server.sockets[0].getsockname()[1]

        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/healthz") as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body == {"status": "ok"}
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_mark_ready_is_idempotent(_slack_token_set):
    healthz.mark_ready()
    healthz.mark_ready()
    status, _ = await _get_healthz(healthz.build_app())
    assert status == 200
