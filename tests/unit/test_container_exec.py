"""Unit tests for router/container_exec.py's lifecycle helpers (#710).

``run_in_container`` (exec) is already exercised indirectly by every module
that mocks it (dispatcher, memory_curator, ...). These tests cover the new
lifecycle-verb siblings — ``restart_container`` and container enumeration —
by monkeypatching ``asyncio.create_subprocess_exec`` so no Docker daemon is
needed. Fakes use plain ``async def`` + ``MagicMock`` (not ``AsyncMock``),
matching the existing convention in test_dispatcher.py: real coroutines
avoid the unawaited-coroutine ``RuntimeWarning`` that this project's
``filterwarnings = ["error"]`` turns into a test failure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from router.container_exec import (
    DispatchError,
    DispatchTimeoutError,
    RestartDisabledError,
    list_project_containers,
    own_compose_project,
    own_container_name,
    restart_container,
    restart_container_detached,
)

pytestmark = pytest.mark.unit


def _make_fake_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()

    async def _communicate():
        return (stdout, stderr)

    proc.communicate = _communicate
    proc.kill = MagicMock()
    proc.returncode = returncode
    return proc


class TestRestartContainer:
    @pytest.mark.asyncio
    async def test_success_runs_docker_restart(self):
        fake_proc = _make_fake_proc(returncode=0, stdout=b"router\n")
        captured: list = []

        async def fake_create(*args, **kwargs):
            captured.extend(args)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            stdout, stderr, rc = await restart_container("router", enabled=True)

        assert captured == ["docker", "restart", "router"]
        assert stdout == "router\n"
        assert stderr == ""
        assert rc == 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_returned_not_raised(self):
        fake_proc = _make_fake_proc(returncode=1, stderr=b"Error: No such container: bogus\n")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            stdout, stderr, rc = await restart_container("bogus", enabled=True)

        assert rc == 1
        assert "No such container" in stderr

    @pytest.mark.asyncio
    async def test_timeout_raises_and_kills_process(self):
        fake_proc = _make_fake_proc(returncode=0)

        async def fake_create(*args, **kwargs):
            return fake_proc

        async def raise_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        with patch("asyncio.create_subprocess_exec", fake_create):
            with patch("asyncio.wait_for", raise_timeout):
                with pytest.raises(DispatchTimeoutError):
                    await restart_container("sam", enabled=True, timeout=1)

        fake_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_refused_when_disabled_no_docker_call(self):
        async def fake_create(*args, **kwargs):
            raise AssertionError("docker should not be invoked when restart is disabled")

        with patch("asyncio.create_subprocess_exec", fake_create):
            with pytest.raises(RestartDisabledError):
                await restart_container("router", enabled=False)


class TestRestartContainerDetached:
    @pytest.mark.asyncio
    async def test_spawns_detached_sleep_and_restart(self):
        fake_proc = _make_fake_proc(returncode=0)
        captured_args: list = []
        captured_kwargs: dict = {}

        async def fake_create(*args, **kwargs):
            captured_args.extend(args)
            captured_kwargs.update(kwargs)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            await restart_container_detached("router", enabled=True, delay=1)

        assert captured_args[0] == "sh"
        assert captured_args[1] == "-c"
        assert "sleep 1" in captured_args[2]
        assert "docker restart router" in captured_args[2]
        assert captured_kwargs.get("start_new_session") is True

    @pytest.mark.asyncio
    async def test_does_not_wait_for_the_child(self):
        """Fire-and-forget: the spawned process's communicate()/wait() is never called."""
        fake_proc = _make_fake_proc(returncode=0)
        fake_proc.wait = MagicMock()
        communicate_calls = []
        real_communicate = fake_proc.communicate

        async def tracking_communicate():
            communicate_calls.append(1)
            return await real_communicate()

        fake_proc.communicate = tracking_communicate

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            await restart_container_detached("router", enabled=True)

        assert communicate_calls == []
        fake_proc.wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_refused_when_disabled_no_process_spawned(self):
        async def fake_create(*args, **kwargs):
            raise AssertionError("no process should be spawned when restart is disabled")

        with patch("asyncio.create_subprocess_exec", fake_create):
            with pytest.raises(RestartDisabledError):
                await restart_container_detached("router", enabled=False)


class TestOwnCompose:
    @pytest.mark.asyncio
    async def test_own_compose_project_returns_label_value(self):
        fake_proc = _make_fake_proc(returncode=0, stdout=b"ai-dev-team\n")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            project = await own_compose_project()

        assert project == "ai-dev-team"

    @pytest.mark.asyncio
    async def test_own_compose_project_none_on_failure(self):
        fake_proc = _make_fake_proc(returncode=1, stderr=b"no such object\n")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            project = await own_compose_project()

        assert project is None

    @pytest.mark.asyncio
    async def test_own_compose_project_none_on_empty_label(self):
        fake_proc = _make_fake_proc(returncode=0, stdout=b"\n")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            project = await own_compose_project()

        assert project is None

    @pytest.mark.asyncio
    async def test_own_container_name_strips_leading_slash(self):
        fake_proc = _make_fake_proc(returncode=0, stdout=b"/router\n")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            name = await own_container_name()

        assert name == "router"


class TestListProjectContainers:
    @pytest.mark.asyncio
    async def test_enumerates_names_from_docker_ps(self):
        inspect_proc = _make_fake_proc(returncode=0, stdout=b"ai-dev-team\n")
        ps_proc = _make_fake_proc(returncode=0, stdout=b"router\nsam\nlisa\n")
        calls: list = []

        async def fake_create(*args, **kwargs):
            calls.append(args)
            return inspect_proc if len(calls) == 1 else ps_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            names = await list_project_containers()

        assert names == ["router", "sam", "lisa"]
        assert calls[1][0:2] == ("docker", "ps")
        assert "label=com.docker.compose.project=ai-dev-team" in calls[1]

    @pytest.mark.asyncio
    async def test_raises_when_own_project_unresolvable(self):
        fake_proc = _make_fake_proc(returncode=1, stderr=b"no such object\n")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            with pytest.raises(DispatchError):
                await list_project_containers()

    @pytest.mark.asyncio
    async def test_raises_when_docker_ps_fails(self):
        inspect_proc = _make_fake_proc(returncode=0, stdout=b"ai-dev-team\n")
        ps_proc = _make_fake_proc(returncode=1, stderr=b"Cannot connect to the Docker daemon\n")
        calls: list = []

        async def fake_create(*args, **kwargs):
            calls.append(args)
            return inspect_proc if len(calls) == 1 else ps_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            with pytest.raises(DispatchError, match="Cannot connect"):
                await list_project_containers()
