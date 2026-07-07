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
