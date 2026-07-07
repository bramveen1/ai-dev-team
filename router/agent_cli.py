"""Shared Claude CLI invocation core (refactoring roadmap §2b).

Both agent-execution seams — ``router.dispatcher.dispatch`` (the legacy
Slack path) and ``router.chat.core.run_agent_turn`` (the transport-neutral
path) — invoke the agent container the same way: same CLI command shape,
same result classification, same guard error classes. This module owns
that contract once; any change to the agent invocation (flags, budgets,
error taxonomy) happens here and both paths pick it up.

``router.dispatcher`` re-exports every moved name for back-compat, so
existing imports and test patch targets keep working.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from router.container_exec import DispatchError
from router.packs.dispatch_hook import PackDispatchExtras

logger = logging.getLogger(__name__)

# System-prompt files inside the agent container. role.md is loaded with
# --system-prompt-file so it REPLACES Claude Code's default identity —
# without this, the default persona dominates and the agent introduces
# itself as Claude Code. The remaining files append on top in order:
# WORLDVIEW -> personality -> agent memory -> org memory.
CONTAINER_WORLDVIEW_FILE = "/config/shared/WORLDVIEW.md"
CONTAINER_ROLE_FILE_TEMPLATE = "/config/agents/{agent}/role.md"
CONTAINER_PERSONALITY_FILE_TEMPLATE = "/config/agents/{agent}/personality.md"
CONTAINER_AGENT_MEMORY_FILE = "/config/agents/{agent}/memory/memory.md"
CONTAINER_ORG_MEMORY_FILE = "/config/shared/MEMORY.md"

# CLI-side budget: max model round-trips per invocation. Paired with the
# router-side wall-clock budget (container_timeout / session_timeout).
# Keep both in sync — see issue #200 and the SESSION_TIMEOUT registry
# default in router/settings.py.
DEFAULT_MAX_TURNS = 50

# Matches "API Error: 529" (case-insensitive) in CLI stderr output.
_API_ERROR_RE = re.compile(r"API Error:\s*(\d+)", re.IGNORECASE)


class ApiError(DispatchError):
    """Raised when the CLI exits due to an HTTP API error (e.g. 429, 529).

    ``status_code`` carries the numeric HTTP status parsed from stderr so
    callers can route 429/529 overload errors to a friendlier user message.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_cli_command(agent_name: str, agent_config: dict, extras: PackDispatchExtras) -> list[str]:
    """Assemble the Claude CLI argv for one agent turn.

    Context is piped via stdin by the caller to avoid shell/CLI argument
    parsing issues (e.g. context starting with "---" being misinterpreted
    as a CLI flag). *extras* comes from ``pack_cli_extras`` — the caller
    resolves it (the Slack path passes channel/thread context, the
    transport-neutral path a conversation_ref) and also forwards
    ``extras.env`` to the container exec.
    """
    cli_cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        "--output-format",
        "json",
        "--system-prompt-file",
        CONTAINER_ROLE_FILE_TEMPLATE.format(agent=agent_name),
        "--append-system-prompt-file",
        CONTAINER_WORLDVIEW_FILE,
        "--append-system-prompt-file",
        CONTAINER_PERSONALITY_FILE_TEMPLATE.format(agent=agent_name),
        "--append-system-prompt-file",
        CONTAINER_AGENT_MEMORY_FILE.format(agent=agent_name),
        "--append-system-prompt-file",
        CONTAINER_ORG_MEMORY_FILE,
        "--no-session-persistence",
        "--max-turns",
        str(DEFAULT_MAX_TURNS),
    ]

    # Per-persona model pin: if agent.yaml declares `model:`, pass it as
    # --model so the persona doesn't default to the CLI's global default.
    agent_model = agent_config.get("model")
    if agent_model:
        cli_cmd += ["--model", agent_model]

    # Pack extras — additive. When agent.yaml has no `packs:` key the
    # extras are empty and the invocation behaves exactly as before.
    for prompt_file in extras.prompt_files:
        cli_cmd += ["--append-system-prompt-file", prompt_file]
    if extras.mcp_config_path:
        cli_cmd += ["--mcp-config", extras.mcp_config_path]

    return cli_cmd


def parse_cli_result(
    *,
    agent_name: str,
    stdout: str,
    stderr: str,
    returncode: int,
    record_error: Callable[[str], None],
) -> tuple[dict, str]:
    """Classify one CLI run's outcome; return ``(payload, response_text)``.

    On any failure, calls ``record_error(error_class)`` exactly once (the
    caller feeds it to the stuck-guard) and raises:

    - ``ApiError`` when stderr carries an "API Error: NNN" marker,
    - ``DispatchError`` for non-zero exit, empty stdout, malformed JSON,
      or an empty ``result`` field.
    """
    if returncode != 0:
        record_error(f"NonZeroExit({returncode})")
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

    if not stdout.strip():
        record_error("EmptyResponse")
        logger.error("Agent %s returned an empty response", agent_name)
        raise DispatchError(f"Agent {agent_name} returned an empty response")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        record_error("InvalidJSON")
        logger.error("Agent %s returned invalid JSON: %s", agent_name, str(e))
        raise DispatchError(f"Agent {agent_name} returned invalid JSON: {e}") from e

    response_text = data.get("result", "")
    if not response_text:
        record_error("EmptyResult")
        logger.error("Agent %s JSON has empty result field", agent_name)
        raise DispatchError(f"Agent {agent_name} returned an empty result")

    return data, response_text


def extract_last_tool_use(cli_payload: dict) -> tuple[str | None, Any]:
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
