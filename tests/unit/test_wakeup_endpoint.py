"""Unit tests for the POST /wakeup endpoint (#323)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from router import healthz
from router.scheduled_tasks.store import ScheduledTaskStore

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_wakeup_store():
    healthz.reset_wakeup_store_for_tests()
    yield
    healthz.reset_wakeup_store_for_tests()


@pytest.fixture(autouse=True)
def _discovered_agents(monkeypatch):
    """/wakeup now accepts any DISCOVERED agent (no hardcoded KNOWN_AGENTS) —
    pin discovery so the tests don't depend on the checkout's config/agents."""
    monkeypatch.setattr("router.config.get_agent_map", lambda: {"sam": {}, "lisa": {}})


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.fixture
def app_with_store(store):
    healthz.set_wakeup_store(store)
    return healthz.build_app()


async def _post_wakeup(app, body: dict):
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/wakeup", json=body)
        data = await resp.json()
        return resp.status, data


_VALID_BODY = {
    "agent": "sam",
    "delay_seconds": 120,
    "reason": "PR #320 CI green",
    "channel": "C0123",
    "thread_ts": "1717999999.000000",
}


@pytest.mark.asyncio
async def test_success_returns_200_with_task_id_and_fire_at(app_with_store, store):
    status, body = await _post_wakeup(app_with_store, _VALID_BODY)

    assert status == 200
    assert "task_id" in body
    assert "fire_at" in body

    task = store.get(body["task_id"])
    assert task is not None
    assert task.agent_name == "sam"
    assert task.one_shot is True
    assert task.payload["channel_id"] == "C0123"
    assert task.payload["thread_ts"] == "1717999999.000000"
    assert task.payload["reason"] == "PR #320 CI green"
    assert task.destination == "C0123"


@pytest.mark.asyncio
async def test_success_fire_at_is_now_plus_delay(app_with_store):
    before = datetime.now(timezone.utc)
    status, body = await _post_wakeup(app_with_store, _VALID_BODY)
    after = datetime.now(timezone.utc)

    assert status == 200
    fire_at = datetime.fromisoformat(body["fire_at"])
    assert before + timedelta(seconds=119) <= fire_at <= after + timedelta(seconds=121)


@pytest.mark.asyncio
async def test_missing_channel_returns_400(app_with_store):
    body = {**_VALID_BODY, "channel": ""}
    status, resp = await _post_wakeup(app_with_store, body)

    assert status == 400
    assert "channel" in resp.get("missing", [])


@pytest.mark.asyncio
async def test_missing_thread_ts_returns_400(app_with_store):
    body = {**_VALID_BODY, "thread_ts": ""}
    status, resp = await _post_wakeup(app_with_store, body)

    assert status == 400
    assert "thread_ts" in resp.get("missing", [])


@pytest.mark.asyncio
async def test_missing_both_channel_and_thread_ts_returns_400(app_with_store):
    body = {k: v for k, v in _VALID_BODY.items() if k not in ("channel", "thread_ts")}
    status, resp = await _post_wakeup(app_with_store, body)

    assert status == 400
    assert set(resp.get("missing", [])) == {"channel", "thread_ts"}


@pytest.mark.asyncio
async def test_unknown_agent_returns_404(app_with_store):
    body = {**_VALID_BODY, "agent": "hal9000"}
    status, resp = await _post_wakeup(app_with_store, body)

    assert status == 404
    assert resp["error"] == "unknown agent"


@pytest.mark.asyncio
async def test_delay_clamped_low(app_with_store, store):
    body = {**_VALID_BODY, "delay_seconds": 5}
    before = datetime.now(timezone.utc)
    status, resp = await _post_wakeup(app_with_store, body)
    after = datetime.now(timezone.utc)

    assert status == 200
    fire_at = datetime.fromisoformat(resp["fire_at"])
    # Should be clamped to 60s, not 5s
    assert fire_at >= before + timedelta(seconds=59)
    assert fire_at <= after + timedelta(seconds=61)


@pytest.mark.asyncio
async def test_delay_clamped_high(app_with_store, store):
    body = {**_VALID_BODY, "delay_seconds": 999999}
    before = datetime.now(timezone.utc)
    status, resp = await _post_wakeup(app_with_store, body)
    after = datetime.now(timezone.utc)

    assert status == 200
    fire_at = datetime.fromisoformat(resp["fire_at"])
    # Should be clamped to 86400s
    assert fire_at <= after + timedelta(seconds=86401)
    assert fire_at >= before + timedelta(seconds=86399)


@pytest.mark.asyncio
async def test_store_error_returns_503():
    broken_store = MagicMock(spec=ScheduledTaskStore)
    broken_store.create_one_shot_task.side_effect = sqlite3.OperationalError("database is locked")
    healthz.set_wakeup_store(broken_store)

    status, resp = await _post_wakeup(healthz.build_app(), _VALID_BODY)

    assert status == 503
    assert resp["error"] == "store unavailable"


@pytest.mark.asyncio
async def test_store_not_injected_returns_503():
    app = healthz.build_app()
    status, resp = await _post_wakeup(app, _VALID_BODY)

    assert status == 503
    assert resp["error"] == "store unavailable"


@pytest.mark.asyncio
async def test_invalid_json_returns_400():
    healthz.set_wakeup_store(MagicMock(spec=ScheduledTaskStore))
    app = healthz.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/wakeup", data=b"not-json", headers={"Content-Type": "application/json"})
        data = await resp.json()
    assert resp.status == 400
    assert "invalid" in data["error"]
