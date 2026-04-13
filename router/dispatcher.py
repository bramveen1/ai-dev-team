"""Router dispatcher — routes messages to agent containers via Claude Code CLI.

Replaces the echo placeholder with real Docker exec invocations to agent
containers running Claude Code CLI. Uses the spike findings from
docs/spike-claude-cli.md for the CLI invocation pattern.
"""

import asyncio
import json
import logging
import time

from router.config import get_agent_map

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
CONTAINER_ROLE_FILE = "/agent/role.md"


class DispatchError(Exception):
    """Raised when an agent dispatch fails (non-zero exit, bad output, etc.)."""


class DispatchTimeoutError(DispatchError):
    """Raised when an agent CLI invocation exceeds the timeout."""


async def _run_in_container(
    container: str,
    command: list[str],
    timeout: int,
) -> tuple[str, str, int]:
    """Execute a command inside a Docker container via ``docker exec``.

    Args:
        container: Docker container name.
        command: Command and arguments to run inside the container.
        timeout: Maximum seconds to wait for the command to finish.

    Returns:
        A tuple of (stdout, stderr, returncode).

    Raises:
        DispatchTimeoutError: If the command does not finish within *timeout*.
    """
    full_cmd = ["docker", "exec", "-u", "claude", container] + command

    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise DispatchTimeoutError(f"Command timed out after {timeout}s in container {container}")

    return stdout_bytes.decode(), stderr_bytes.decode(), proc.returncode


async def dispatch(
    agent_name: str,
    message: str,
    channel: str,
    thread_ts: str,
    client,
    timeout: int | None = None,
) -> dict:
    """Dispatch a message to an agent container and return the response.

    Invokes Claude Code CLI inside the agent's Docker container with the
    agent's role.md as a system prompt append, captures the JSON response,
    and returns a result dict.

    Args:
        agent_name: Logical name of the target agent (e.g. "lisa").
        message: The user's message text.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp for threading replies.
        client: Slack WebClient instance (reserved for future use).
        timeout: Optional timeout in seconds for the CLI call.
            Defaults to DEFAULT_TIMEOUT_SECONDS (30s).

    Returns:
        A dict with keys:
            - agent: The agent name that handled the request.
            - status: "ok" on success.
            - response: The agent's response text.

    Raises:
        ValueError: If agent_name is not in the agent map or message is empty.
        DispatchTimeoutError: If the CLI call exceeds the timeout.
        DispatchError: If the CLI exits non-zero, returns empty/invalid output.
    """
    if not message or not message.strip():
        raise ValueError("Message must not be empty")

    agent_map = get_agent_map()
    if agent_name not in agent_map:
        raise ValueError(f"Unknown agent: {agent_name}")

    agent_config = agent_map[agent_name]
    container = agent_config["container"]
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS

    logger.info(
        "Dispatching to agent=%s container=%s msg_len=%d timeout=%ds",
        agent_name,
        container,
        len(message),
        effective_timeout,
    )

    start_time = time.monotonic()

    # Build Claude CLI command (per spike-claude-cli.md recommended defaults)
    cli_cmd = [
        "claude",
        "-p",
        message,
        "--output-format",
        "json",
        "--append-system-prompt-file",
        CONTAINER_ROLE_FILE,
        "--no-session-persistence",
        "--max-turns",
        "1",
    ]

    stdout, stderr, returncode = await _run_in_container(
        container,
        cli_cmd,
        effective_timeout,
    )

    duration = time.monotonic() - start_time

    # Handle non-zero exit code
    if returncode != 0:
        logger.error(
            "Agent %s CLI exited with code %d stdout=%s stderr=%s",
            agent_name,
            returncode,
            stdout[:500],
            stderr[:500],
        )
        raise DispatchError(f"Agent {agent_name} CLI exited with code {returncode}: {stdout[:200]} {stderr[:200]}")

    # Handle empty stdout
    if not stdout.strip():
        logger.error("Agent %s returned empty response after %.2fs", agent_name, duration)
        raise DispatchError(f"Agent {agent_name} returned an empty response")

    # Parse JSON output
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.error("Agent %s returned invalid JSON: %s", agent_name, str(e))
        raise DispatchError(f"Agent {agent_name} returned invalid JSON: {e}")

    response_text = data.get("result", "")
    if not response_text:
        logger.error("Agent %s JSON has empty result field", agent_name)
        raise DispatchError(f"Agent {agent_name} returned an empty result")

    logger.info(
        "Agent %s responded: response_len=%d duration=%.2fs",
        agent_name,
        len(response_text),
        duration,
    )

    return {
        "agent": agent_name,
        "status": "ok",
        "response": response_text,
    }
