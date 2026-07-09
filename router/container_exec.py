"""Run a command inside an agent's Docker container — leaf module.

Extracted from ``router.dispatcher`` (refactoring roadmap §2b) so callers
that only need docker-exec (session-end extraction, memory curation, the
auto-dispatch worker) don't drag in the full dispatch import tree.
``router.dispatcher`` re-exports every name here, so existing
``from router.dispatcher import _run_in_container`` call sites and test
patch targets keep working unchanged.

This module must stay a leaf: stdlib imports only.
"""

from __future__ import annotations

import asyncio
import shlex
import socket


class DispatchError(Exception):
    """Raised when an agent dispatch fails (non-zero exit, bad output, etc.)."""


class DispatchTimeoutError(DispatchError):
    """Raised when an agent CLI invocation exceeds the timeout."""


async def run_in_container(
    container: str,
    command: list[str],
    timeout: int,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str, int]:
    """Execute a command inside a Docker container via ``docker exec``.

    Args:
        container: Docker container name.
        command: Command and arguments to run inside the container.
        timeout: Maximum seconds to wait for the command to finish.
        stdin_data: Optional string to pipe to the process's stdin.
        env: Optional env vars passed to ``docker exec`` via ``-e KEY=VALUE``.

    Returns:
        A tuple of (stdout, stderr, returncode).

    Raises:
        DispatchTimeoutError: If the command does not finish within *timeout*.
    """
    full_cmd = ["docker", "exec"]
    if stdin_data is not None:
        full_cmd.append("-i")
    if env:
        for key, value in env.items():
            full_cmd += ["-e", f"{key}={value}"]
    # Wrap with coreutils timeout(1) so the kill happens inside the container's
    # PID namespace — prevents orphaned claude -p processes if the router-side
    # asyncio timeout fires and kills only the local docker exec client.
    full_cmd += ["-u", "claude", container, "timeout", str(timeout)] + command

    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdin_bytes = stdin_data.encode() if stdin_data is not None else None
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise DispatchTimeoutError(f"Command timed out after {timeout}s in container {container}")

    # coreutils timeout(1) exits 124 when the wrapped command was killed on expiry;
    # map that to DispatchTimeoutError to preserve the existing error surface.
    if proc.returncode == 124:
        raise DispatchTimeoutError(f"Command timed out after {timeout}s in container {container}")

    return stdout_bytes.decode(), stderr_bytes.decode(), proc.returncode


async def _run_docker(args: list[str], timeout: int) -> tuple[str, str, int]:
    """Run a bare ``docker`` CLI command against the mounted socket."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise DispatchTimeoutError(f"docker {' '.join(args)} timed out after {timeout}s")
    return stdout_bytes.decode(), stderr_bytes.decode(), proc.returncode


async def own_compose_project(timeout: int = 10) -> str | None:
    """The ``com.docker.compose.project`` label on this process's own container.

    Resolved via ``docker inspect`` on our own hostname rather than a
    hardcoded project name — Docker sets a container's hostname to its
    short ID by default, so this works across deployments without extra
    configuration. Returns ``None`` if the lookup fails (no socket, not
    running under compose, etc.).
    """
    stdout, _stderr, rc = await _run_docker(
        ["inspect", "--format", '{{ index .Config.Labels "com.docker.compose.project" }}', socket.gethostname()],
        timeout=timeout,
    )
    if rc != 0:
        return None
    return stdout.strip() or None


async def own_container_name(timeout: int = 10) -> str | None:
    """This process's own container name — used to special-case self-restart."""
    stdout, _stderr, rc = await _run_docker(["inspect", "--format", "{{.Name}}", socket.gethostname()], timeout=timeout)
    if rc != 0:
        return None
    return stdout.strip().lstrip("/") or None


async def list_project_containers(timeout: int = 10) -> list[str]:
    """Enumerate live container names in this compose project.

    Source of truth is Docker itself (``docker ps``), not a static list, so
    a newly added agent container shows up with no code change. Used both
    to render the Containers card and as the allowlist that
    :func:`restart_container` callers validate a name against before
    shelling out.
    """
    project = await own_compose_project(timeout=timeout)
    if not project:
        raise DispatchError("could not resolve this container's compose project (docker inspect failed)")
    stdout, stderr, rc = await _run_docker(
        ["ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Names}}"],
        timeout=timeout,
    )
    if rc != 0:
        raise DispatchError(f"docker ps failed: {stderr.strip() or rc}")
    return [line for line in stdout.splitlines() if line.strip()]


async def restart_container(name: str, timeout: int = 30) -> tuple[str, str, int]:
    """Restart a Docker container by name — ``docker restart <name>``.

    A thin sibling of :func:`run_in_container`, not a reuse of it: this is
    a lifecycle verb against the socket, not an exec, so it skips the
    ``-u claude ... timeout(1)`` wrapping that shape hard-codes.

    Returns (stdout, stderr, returncode) — the caller decides how to surface
    a non-zero exit. Raises :class:`DispatchTimeoutError` if the daemon
    doesn't finish the restart within *timeout* seconds.
    """
    return await _run_docker(["restart", name], timeout=timeout)


async def restart_container_detached(name: str, delay: int = 1) -> None:
    """Fire-and-forget restart, for the router-restarting-itself race.

    ``docker restart <self>`` SIGTERMs this very process, so a normal
    awaited restart can kill the router before its HTTP response flushes.
    Instead, spawn a detached ``sleep <delay>; docker restart <name>`` in a
    new session and return without waiting — the caller's 202 response
    reaches the client while this process is still alive to send it.
    """
    await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        f"sleep {delay}; docker restart {shlex.quote(name)}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
