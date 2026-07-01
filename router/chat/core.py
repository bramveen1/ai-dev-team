"""Transport-agnostic core seam for the ChatAdapter contract.

``run_agent_turn`` is the single entry-point through which any adapter invokes
an agent. It contains no transport-specific logic: no Slack client, no channel
ID, no thread_ts. Adapters supply thread history and receive the reply through
the ChatAdapter primitives only.
"""

from __future__ import annotations

import json
import logging

from router.chat.interface import ChatAdapter
from router.chat.types import AdapterStatus, InboundMessage, OutboundMessage
from router.config import get_agent_map
from router.context_builder import build_full_context
from router.dispatcher import (
    CONTAINER_AGENT_MEMORY_FILE,
    CONTAINER_ORG_MEMORY_FILE,
    CONTAINER_PERSONALITY_FILE_TEMPLATE,
    CONTAINER_ROLE_FILE_TEMPLATE,
    CONTAINER_WORLDVIEW_FILE,
    DEFAULT_MAX_TOKEN_BUDGET,
    DispatchError,
    _run_in_container,
)
from router.memory_loader import load_agent_memory
from router.session_manager import DEFAULT_TIMEOUT_SECONDS as AGENT_TURN_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "sam"


async def run_agent_turn(
    adapter: ChatAdapter,
    inbound: InboundMessage,
    *,
    agent_name: str | None = None,
    timeout: int = AGENT_TURN_TIMEOUT_SECONDS,
    max_token_budget: int = DEFAULT_MAX_TOKEN_BUDGET,
) -> str:
    """Route one inbound message through an agent container and return the reply.

    Transport-agnostic: every reference to the transport goes through
    ``adapter``. No Slack client, channel, or thread_ts appear here.

    Resolution order for the agent:
        1. Explicit ``agent_name`` kwarg (caller override).
        2. First ``@mention`` extracted by ``adapter.parse_mentions()``.
        3. ``_DEFAULT_AGENT`` ("sam").

    Args:
        adapter: Any ChatAdapter implementation (terminal, Slack stub, …).
        inbound: The inbound message to process.
        agent_name: Optional agent override. When ``None``, mentions are parsed
            from ``inbound.text`` and the first one is used.
        timeout: Wall-clock budget in seconds for the CLI invocation.
        max_token_budget: Maximum context tokens passed to build_full_context.

    Returns:
        The agent's reply text. The same text is also delivered to the
        transport via ``adapter.send_message``.

    Raises:
        ValueError: If the resolved agent name is not in the agent map.
        DispatchError: If the CLI exits non-zero, returns empty output, or
            returns malformed JSON.
    """
    await adapter.set_status(inbound.conversation_ref, AdapterStatus.THINKING)

    # Resolve agent: explicit override > first @mention > default
    if agent_name is None:
        mentions = adapter.parse_mentions(inbound.text, inbound.conversation_ref)
        agent_name = mentions[0] if mentions else _DEFAULT_AGENT

    agent_map = get_agent_map()
    if agent_name not in agent_map:
        raise ValueError(f"Unknown agent: {agent_name}")

    agent_config = agent_map[agent_name]
    container = agent_config["container"]
    display_name = agent_config.get("name", agent_name.capitalize())
    effective_timeout = agent_config.get("container_timeout") or timeout

    # Load thread history via adapter (not via Slack API)
    history_messages = await adapter.read_thread(inbound.conversation_ref)
    thread_history_dicts = [{"user": str(msg.principal_ref), "text": msg.text, "ts": ""} for msg in history_messages]

    # Build context: memory + history + new message
    memory = load_agent_memory(agent_name)
    context = build_full_context(
        memory=memory,
        thread_history=thread_history_dicts,
        new_message=inbound.text,
        agent_name=display_name,
        max_tokens=max_token_budget,
    )

    # Assemble the Claude CLI invocation (identical shape to dispatcher.py)
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

    agent_model = agent_config.get("model")
    if agent_model:
        cli_cmd += ["--model", agent_model]

    logger.info("run_agent_turn agent=%s container=%s timeout=%ds", agent_name, container, effective_timeout)

    stdout, stderr, returncode = await _run_in_container(
        container,
        cli_cmd,
        effective_timeout,
        stdin_data=context,
    )

    if returncode != 0:
        logger.error(
            "Agent %s CLI exited with code %d stderr=%s",
            agent_name,
            returncode,
            stderr[:500],
        )
        raise DispatchError(f"Agent {agent_name} CLI exited with code {returncode}: {stderr[:200]}")

    if not stdout.strip():
        raise DispatchError(f"Agent {agent_name} returned an empty response")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"Agent {agent_name} returned invalid JSON: {exc}") from exc

    response_text = data.get("result", "")
    if not response_text:
        raise DispatchError(f"Agent {agent_name} returned an empty result")

    await adapter.send_message(OutboundMessage(text=response_text, conversation_ref=inbound.conversation_ref))
    await adapter.set_status(inbound.conversation_ref, AdapterStatus.DONE)

    logger.info("run_agent_turn agent=%s response_len=%d", agent_name, len(response_text))
    return response_text
