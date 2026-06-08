"""Router dispatcher — routes messages to agent containers via Claude Code CLI.

Replaces the echo placeholder with real Docker exec invocations to agent
containers running Claude Code CLI. Uses the spike findings from
docs/spike-claude-cli.md for the CLI invocation pattern.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from router.config import get_agent_map
from router.context_builder import build_full_context
from router.memory_loader import load_agent_memory
from router.packs.dispatch_hook import pack_cli_extras
from router.stuck_guard import (
    GuardTrip,
    StuckGuard,
    format_slack_message,
    get_default_guard,
    make_task_id,
    write_post_mortem,
)
from router.thread_loader import load_thread_history, split_messages_at_summary

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_TOKEN_BUDGET = 32000
DEFAULT_MAX_THREAD_MESSAGES = 20
MAX_CONTEXT_TOKENS_ENV = "MAX_CONTEXT_TOKENS"
CONTAINER_WORLDVIEW_FILE = "/config/shared/WORLDVIEW.md"
CONTAINER_ROLE_FILE_TEMPLATE = "/config/agents/{agent}/role.md"
CONTAINER_PERSONALITY_FILE_TEMPLATE = "/config/agents/{agent}/personality.md"
CONTAINER_AGENT_MEMORY_FILE = "/config/agents/{agent}/memory/memory.md"
CONTAINER_ORG_MEMORY_FILE = "/config/shared/MEMORY.md"


class DispatchError(Exception):
    """Raised when an agent dispatch fails (non-zero exit, bad output, etc.)."""


class DispatchTimeoutError(DispatchError):
    """Raised when an agent CLI invocation exceeds the timeout."""


class ApiError(DispatchError):
    """Raised when the CLI exits due to an HTTP API error (e.g. 429, 529).

    ``status_code`` carries the numeric HTTP status parsed from stderr so
    callers can route 429/529 overload errors to a friendlier user message.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class TaskHaltedError(DispatchError):
    """Raised when the stuck-guard has halted this task and a new dispatch
    is attempted before the task is reset."""

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"Task {task_id} halted by stuck-guard: {reason}")
        self.task_id = task_id
        self.reason = reason


# Matches "API Error: 529" (case-insensitive) in CLI stderr output.
_API_ERROR_RE = re.compile(r"API Error:\s*(\d+)", re.IGNORECASE)


def _resolve_token_budget(explicit_budget: int | None) -> int:
    """Resolve the effective token budget for a dispatch.

    Precedence: explicit arg > ``MAX_CONTEXT_TOKENS`` env var > default.
    Invalid env values (non-int or non-positive) log a warning and fall back
    to ``DEFAULT_MAX_TOKEN_BUDGET``.
    """
    if explicit_budget is not None:
        return explicit_budget

    raw = os.environ.get(MAX_CONTEXT_TOKENS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_TOKEN_BUDGET

    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (not an int); falling back to default %d",
            MAX_CONTEXT_TOKENS_ENV,
            raw,
            DEFAULT_MAX_TOKEN_BUDGET,
        )
        return DEFAULT_MAX_TOKEN_BUDGET

    if parsed <= 0:
        logger.warning(
            "Invalid %s=%d (must be > 0); falling back to default %d",
            MAX_CONTEXT_TOKENS_ENV,
            parsed,
            DEFAULT_MAX_TOKEN_BUDGET,
        )
        return DEFAULT_MAX_TOKEN_BUDGET

    return parsed


async def _run_in_container(
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
    full_cmd += ["-u", "claude", container] + command

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

    return stdout_bytes.decode(), stderr_bytes.decode(), proc.returncode


def _slack_thread_url(client, channel: str, thread_ts: str) -> str | None:
    """Best-effort Slack permalink for the post-mortem footer.

    Returns ``None`` if the client doesn't expose a permalink helper or
    the call fails — we never want post-mortem writing to crash because
    Slack's API blipped.
    """
    fetch = getattr(client, "chat_getPermalink", None)
    if fetch is None:
        return None
    try:
        resp = fetch(channel=channel, message_ts=thread_ts)
    except Exception:
        return None
    if hasattr(resp, "data"):
        resp = resp.data
    if isinstance(resp, dict):
        return resp.get("permalink")
    return None


async def _post_stuck_notification(
    *,
    client,
    channel: str,
    thread_ts: str,
    text: str,
) -> None:
    """Post the stuck-guard Slack note in the originating thread.

    Errors are swallowed — failing to notify on Slack must not mask the
    underlying trip from the dispatcher's caller.
    """
    poster = getattr(client, "chat_postMessage", None)
    if poster is None:
        return
    try:
        result = poster(channel=channel, thread_ts=thread_ts, text=text)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("Failed to post stuck-guard Slack notification")


def _handle_guard_trip(
    *,
    guard: StuckGuard,
    trip: GuardTrip,
    task_id: str,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client,
    task_description: str | None,
) -> None:
    """Write the post-mortem and notify Slack in-thread.

    Synchronous on disk; Slack notification is fire-and-forget so the
    network round-trip can't block the next dispatch. Exceptions are
    logged and swallowed — a guard trip already implies we're in a bad
    state, so a notification failure must not cascade.
    """
    state = guard.get_state(task_id)
    if state is None:
        logger.warning("Guard trip with no state for task=%s; skipping post-mortem", task_id)
        return

    try:
        thread_url = _slack_thread_url(client, channel, thread_ts)
    except Exception:
        logger.debug("Could not resolve Slack thread URL for post-mortem", exc_info=True)
        thread_url = None

    try:
        path = write_post_mortem(
            state=state,
            trip=trip,
            config=guard.config,
            slack_thread_url=thread_url,
            task_description=task_description,
        )
    except Exception:
        logger.exception("Failed to write stuck-guard post-mortem")
        path = None

    text = format_slack_message(state=state, trip=trip, post_mortem_path=path, config=guard.config)
    asyncio.ensure_future(_post_stuck_notification(client=client, channel=channel, thread_ts=thread_ts, text=text))


async def dispatch(
    agent_name: str,
    message: str,
    channel: str,
    thread_ts: str,
    client,
    timeout: int | None = None,
    max_token_budget: int | None = None,
    max_thread_messages: int | None = None,
    bot_user_map: dict[str, str] | None = None,
    guard: StuckGuard | None = None,
) -> dict:
    """Dispatch a message to an agent container and return the response.

    Loads thread history from Slack, loads agent memory, builds a full
    context (with session resume support), and invokes Claude Code CLI
    inside the agent's Docker container with the agent's role.md as the
    system prompt (replacing Claude Code's default identity). Captures the
    JSON response and returns a result dict.

    Args:
        agent_name: Logical name of the target agent (e.g. "lisa").
        message: The user's message text.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp for threading replies.
        client: Slack WebClient instance (used to fetch thread history).
        timeout: Optional timeout in seconds for the CLI call.
            Defaults to DEFAULT_TIMEOUT_SECONDS (30s).
        max_token_budget: Maximum token budget for conversation context.
            When ``None``, resolves from the ``MAX_CONTEXT_TOKENS`` env var
            and falls back to DEFAULT_MAX_TOKEN_BUDGET (32000).
        max_thread_messages: Maximum thread messages to load.
            Defaults to DEFAULT_MAX_THREAD_MESSAGES (20).

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
    display_name = agent_config.get("name", agent_name.capitalize())
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS
    effective_budget = _resolve_token_budget(max_token_budget)
    effective_max_messages = max_thread_messages if max_thread_messages is not None else DEFAULT_MAX_THREAD_MESSAGES

    # Stuck-guard pre-check. We always honor a halted task — in enforce
    # mode that means "trip detected, stop the bleed", and in dry-run mode
    # only `/kill` can flip `halted` (regular trips just record without
    # halting per spec). So a halted state in either mode means: a human
    # asked us to stop, or the guard caught a runaway in enforce. Either
    # way, refuse to dispatch.
    active_guard = guard if guard is not None else get_default_guard()
    task_id = make_task_id(channel, thread_ts, agent_name)
    if active_guard.is_halted(task_id):
        state = active_guard.get_state(task_id)
        reason = state.halt_reason.description if state and state.halt_reason else "halted"
        logger.warning("Refusing dispatch — task=%s halted by stuck-guard: %s", task_id, reason)
        raise TaskHaltedError(task_id, reason)

    logger.info(
        "Dispatching to agent=%s container=%s msg_len=%d timeout=%ds",
        agent_name,
        container,
        len(message),
        effective_timeout,
    )

    start_time = time.monotonic()

    # Load thread history from Slack
    thread_history = await load_thread_history(
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        max_messages=effective_max_messages,
    )

    # Check for session summary in thread history (resume from timeout)
    session_summary = None
    context_history = thread_history

    if thread_history:
        session_summary, context_history = split_messages_at_summary(thread_history)
        if session_summary:
            logger.info("Resuming from session summary for agent=%s", agent_name)

    # Load memory context for the agent
    memory = load_agent_memory(agent_name)

    # Resolve bot_user_map agent IDs to their display names so the
    # transcript labels each agent's messages correctly after handoffs.
    display_bot_user_map: dict[str, str] | None = None
    if bot_user_map:
        display_bot_user_map = {
            user_id: agent_map.get(name, {}).get("name", name.capitalize())
            for user_id, name in bot_user_map.items()
            if name in agent_map
        }

    # Build full context with memory + thread history + new message
    context = build_full_context(
        memory=memory,
        thread_history=context_history,
        new_message=message,
        agent_name=display_name,
        session_summary=session_summary,
        max_tokens=effective_budget,
        bot_user_map=display_bot_user_map,
    )

    logger.info("Built context with %d thread messages for agent=%s", len(thread_history), agent_name)

    # Build Claude CLI command (per spike-claude-cli.md recommended defaults)
    # role.md is loaded with --system-prompt-file so it REPLACES Claude Code's
    # default "You are Claude Code" identity — without this, the default
    # persona dominates and the agent introduces itself as Claude Code.
    # The remaining files append on top in order:
    # WORLDVIEW -> personality -> agent memory -> org memory.
    # Context is piped via stdin to avoid shell/CLI argument parsing issues
    # (e.g. context starting with "---" being misinterpreted as a CLI flag)
    role_file = CONTAINER_ROLE_FILE_TEMPLATE.format(agent=agent_name)
    personality_file = CONTAINER_PERSONALITY_FILE_TEMPLATE.format(agent=agent_name)
    agent_memory_file = CONTAINER_AGENT_MEMORY_FILE.format(agent=agent_name)

    cli_cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        "--output-format",
        "json",
        "--system-prompt-file",
        role_file,
        "--append-system-prompt-file",
        CONTAINER_WORLDVIEW_FILE,
        "--append-system-prompt-file",
        personality_file,
        "--append-system-prompt-file",
        agent_memory_file,
        "--append-system-prompt-file",
        CONTAINER_ORG_MEMORY_FILE,
        "--no-session-persistence",
        "--max-turns",
        "50",
    ]

    # Pack extras — additive. When agent.yaml has no `packs:` key the
    # extras are empty and dispatch behaves exactly as before. Slack
    # context flows through so the dispatch pack can inject
    # DISPATCH_CHANNEL/THREAD_TS/AGENT for agent-initiated dispatches.
    extras = pack_cli_extras(agent_name, channel=channel, thread_ts=thread_ts)
    for prompt_file in extras.prompt_files:
        cli_cmd += ["--append-system-prompt-file", prompt_file]
    if extras.mcp_config_path:
        cli_cmd += ["--mcp-config", extras.mcp_config_path]

    logger.info("CLI command for agent=%s: %s", agent_name, " ".join(cli_cmd))

    error_class: str | None = None
    try:
        stdout, stderr, returncode = await _run_in_container(
            container,
            cli_cmd,
            effective_timeout,
            stdin_data=context,
            env=extras.env or None,
        )
    except DispatchTimeoutError:
        error_class = "DispatchTimeoutError"
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
        )
        raise

    duration = time.monotonic() - start_time

    # Handle non-zero exit code
    if returncode != 0:
        error_class = f"NonZeroExit({returncode})"
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
        )
        logger.error(
            "Agent %s CLI exited with code %d stdout=%s stderr=%s",
            agent_name,
            returncode,
            stdout[:500],
            stderr[:500],
        )
        m = _API_ERROR_RE.search(stderr)
        if m:
            status_code = int(m.group(1))
            raise ApiError(status_code, f"Agent {agent_name} CLI API error {status_code}")
        raise DispatchError(f"Agent {agent_name} CLI exited with code {returncode}: {stdout[:200]} {stderr[:200]}")

    # Handle empty stdout
    if not stdout.strip():
        error_class = "EmptyResponse"
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
        )
        logger.error("Agent %s returned empty response after %.2fs", agent_name, duration)
        raise DispatchError(f"Agent {agent_name} returned an empty response")

    # Parse JSON output
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        error_class = "InvalidJSON"
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
        )
        logger.error("Agent %s returned invalid JSON: %s", agent_name, str(e))
        raise DispatchError(f"Agent {agent_name} returned invalid JSON: {e}")

    response_text = data.get("result", "")
    if not response_text:
        error_class = "EmptyResult"
        _record_and_handle_trip(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            task_description=message,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
        )
        logger.error("Agent %s JSON has empty result field", agent_name)
        raise DispatchError(f"Agent {agent_name} returned an empty result")

    # Successful turn — record it for guard accounting. Tool name/args come
    # from the CLI's reported last tool use when available; otherwise we
    # log a "text" turn (still counts toward the turn cap).
    last_tool_name, last_tool_args = _extract_last_tool_use(data)
    _record_and_handle_trip(
        guard=active_guard,
        task_id=task_id,
        agent_name=agent_name,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        task_description=message,
        tool_name=last_tool_name,
        tool_args=last_tool_args,
        error_class=None,
    )

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


def _extract_last_tool_use(cli_payload: dict) -> tuple[str | None, object | None]:
    """Pull the most recent tool name + args from a Claude CLI JSON payload.

    The CLI sometimes includes a ``messages`` transcript with ``tool_use``
    blocks. When it does, we use the last one for loop detection. When
    it doesn't, we return ``(None, None)`` and the turn is recorded as
    pure text — turn-cap and error-streak guards still work, only loop
    detection is downgraded.
    """
    messages = cli_payload.get("messages")
    if not isinstance(messages, list):
        return None, None
    for msg in reversed(messages):
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                return block.get("name"), block.get("input")
    return None, None


def _record_and_handle_trip(
    *,
    guard: StuckGuard,
    task_id: str,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client,
    task_description: str | None,
    tool_name: str | None,
    tool_args: object,
    error_class: str | None,
) -> None:
    """Feed the turn to the guard and run the trip handler if it fired.

    Pulled out so the dispatch error paths and the success path share
    the same handling — every turn the dispatcher observes (good or bad)
    counts toward the guard's accounting.
    """
    trip = guard.record_turn(
        task_id=task_id,
        agent_name=agent_name,
        tool_name=tool_name,
        tool_args=tool_args,
        error_class=error_class,
    )
    if trip is None:
        return
    _handle_guard_trip(
        guard=guard,
        trip=trip,
        task_id=task_id,
        agent_name=agent_name,
        channel=channel,
        thread_ts=thread_ts,
        client=client,
        task_description=task_description,
    )
