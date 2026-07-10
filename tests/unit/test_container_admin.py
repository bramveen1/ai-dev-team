"""Unit tests for router/container_admin.py — Containers API for the /config page (#710)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from router import container_admin
from router.container_exec import DispatchError, DispatchTimeoutError

pytestmark = pytest.mark.unit


def _build_app() -> web.Application:
    app = web.Application()
    container_admin.register_routes(app)
    return app


async def _client():
    return TestClient(TestServer(_build_app()))


class TestListContainers:
    @pytest.mark.asyncio
    async def test_lists_containers_and_flags_self(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam", "lisa", "browser-use"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch("router.container_admin._restart_enabled", return_value=True),
        ):
            async with await _client() as client:
                resp = await client.get("/config/api/containers")
                body = await resp.json()

        assert resp.status == 200
        assert body["self"] == "router"
        assert body["restart_enabled"] is True
        by_name = {c["name"]: c for c in body["containers"]}
        assert by_name["router"]["self"] is True
        assert by_name["sam"]["self"] is False
        assert set(by_name) == {"router", "sam", "lisa", "browser-use"}

    @pytest.mark.asyncio
    async def test_restart_enabled_reflects_flag_off(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch("router.container_admin._restart_enabled", return_value=False),
        ):
            async with await _client() as client:
                resp = await client.get("/config/api/containers")
                body = await resp.json()

        assert resp.status == 200
        assert body["restart_enabled"] is False

    @pytest.mark.asyncio
    async def test_500_when_project_unresolvable(self):
        with patch(
            "router.container_admin.container_exec.list_project_containers",
            new_callable=AsyncMock,
            side_effect=DispatchError("could not resolve this container's compose project"),
        ):
            async with await _client() as client:
                resp = await client.get("/config/api/containers")
                body = await resp.json()

        assert resp.status == 500
        assert "compose project" in body["error"]


class TestRestartContainer:
    @pytest.mark.asyncio
    async def test_400_on_unknown_name(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ),
            patch("router.container_admin._restart_enabled", return_value=True),
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/off-project/restart")
                body = await resp.json()

        assert resp.status == 400
        assert "off-project" in body["error"]

    @pytest.mark.asyncio
    async def test_self_restart_is_detached_and_202(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch(
                "router.container_admin.container_exec.restart_container_detached",
                new_callable=AsyncMock,
            ) as mock_detached,
            patch(
                "router.container_admin.container_exec.restart_container",
                new_callable=AsyncMock,
            ) as mock_restart,
            patch("router.container_admin._restart_enabled", return_value=True),
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/router/restart")
                body = await resp.json()

        assert resp.status == 202
        assert body == {"ok": True, "name": "router", "self_restart": True}
        mock_detached.assert_called_once_with("router", enabled=True)
        mock_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_self_restart_awaits_and_202_on_success(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch(
                "router.container_admin.container_exec.restart_container",
                new_callable=AsyncMock,
                return_value=("sam\n", "", 0),
            ) as mock_restart,
            patch("router.container_admin._restart_enabled", return_value=True),
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/sam/restart")
                body = await resp.json()

        assert resp.status == 202
        assert body == {"ok": True, "name": "sam", "self_restart": False}
        mock_restart.assert_called_once_with("sam", enabled=True)

    @pytest.mark.asyncio
    async def test_500_with_stderr_on_daemon_error(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch(
                "router.container_admin.container_exec.restart_container",
                new_callable=AsyncMock,
                return_value=("", "Cannot connect to the Docker daemon\n", 1),
            ),
            patch("router.container_admin._restart_enabled", return_value=True),
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/sam/restart")
                body = await resp.json()

        assert resp.status == 500
        assert "Cannot connect to the Docker daemon" in body["error"]

    @pytest.mark.asyncio
    async def test_500_on_timeout(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch(
                "router.container_admin.container_exec.restart_container",
                new_callable=AsyncMock,
                side_effect=DispatchTimeoutError("docker restart sam timed out after 30s"),
            ),
            patch("router.container_admin._restart_enabled", return_value=True),
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/sam/restart")
                body = await resp.json()

        assert resp.status == 500
        assert "timed out" in body["error"]


class TestRestartDisabledFlag:
    @pytest.mark.asyncio
    async def test_403_when_flag_off_default(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ) as mock_list,
            patch(
                "router.container_admin.container_exec.restart_container",
                new_callable=AsyncMock,
            ) as mock_restart,
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/sam/restart")
                body = await resp.json()

        assert resp.status == 403
        assert "disabled" in body["error"]
        assert "CONFIG_CONTAINER_RESTART_ENABLED" in body["error"]
        mock_restart.assert_not_called()
        # Refused before even enumerating containers — no unnecessary docker calls.
        mock_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_403_when_flag_explicitly_disabled(self):
        with (
            patch(
                "router.container_admin.container_exec.list_project_containers",
                new_callable=AsyncMock,
                return_value=["router", "sam"],
            ),
            patch(
                "router.container_admin.container_exec.own_container_name",
                new_callable=AsyncMock,
                return_value="router",
            ),
            patch(
                "router.container_admin.container_exec.restart_container_detached",
                new_callable=AsyncMock,
            ) as mock_detached,
            patch("router.container_admin._restart_enabled", return_value=False),
        ):
            async with await _client() as client:
                resp = await client.post("/config/api/containers/router/restart")
                body = await resp.json()

        assert resp.status == 403
        assert "disabled" in body["error"]
        mock_detached.assert_not_called()
