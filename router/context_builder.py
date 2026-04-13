"""Context builder — assembles and manages context for agent dispatch.

Builds a full context string from role definitions, memory, thread history,
and system documentation. Provides token estimation and truncation to stay
within budget limits.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Approximate tokens-per-character ratio (conservative: ~4 chars per token)
_CHARS_PER_TOKEN = 4

TRUNCATION_MARKER = "[...earlier messages truncated...]"


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text string.

    Uses a rough heuristic of ~4 characters per token.

    Args:
        text: The text to estimate.

    Returns:
        Estimated token count as an integer.
    """
    if not text:
        return 0
    return len(text) // _CHARS_PER_TOKEN


def truncate_to_budget(text: str, max_tokens: int) -> str:
    """Truncate text to fit within a token budget.

    If the text is already within budget, returns it unchanged.
    Otherwise, truncates from the beginning (keeping the most recent content)
    and prepends a truncation marker.

    Args:
        text: The text to truncate.
        max_tokens: Maximum allowed tokens.

    Returns:
        The text, possibly truncated with a marker prepended.
    """
    if estimate_tokens(text) <= max_tokens:
        return text

    # Reserve space for the truncation marker
    marker_tokens = estimate_tokens(TRUNCATION_MARKER + "\n")
    available_tokens = max_tokens - marker_tokens
    if available_tokens <= 0:
        return TRUNCATION_MARKER

    # Keep the tail of the text (most recent content)
    max_chars = available_tokens * _CHARS_PER_TOKEN
    truncated = text[-max_chars:]

    # Try to break at a newline to avoid cutting mid-line
    newline_pos = truncated.find("\n")
    if newline_pos != -1 and newline_pos < len(truncated) // 2:
        truncated = truncated[newline_pos + 1 :]

    return TRUNCATION_MARKER + "\n" + truncated


def build_conversation_context(
    thread_history: list[dict],
    bot_user_id: str | None = None,
    agent_name: str = "Lisa",
) -> str:
    """Format thread history as a readable conversation transcript.

    Maps bot messages to the agent name and human messages to their
    display name or user ID.

    Args:
        thread_history: Parsed thread messages (list of dicts with user/text/ts).
        bot_user_id: The Slack bot user ID, used to identify agent messages.
        agent_name: Display name for the agent (default: "Lisa").

    Returns:
        A formatted transcript string, e.g.:
        "[User]: Can you check my calendar?\n[Lisa]: You have 3 meetings..."
    """
    if not thread_history:
        return ""

    lines = []
    for msg in thread_history:
        user = msg.get("user", "unknown")
        text = msg.get("text", "")

        if bot_user_id and user == bot_user_id:
            speaker = agent_name
        elif user.startswith("U_BOT") or user.startswith("B"):
            # Heuristic: bot user IDs often start with B, test fixtures use U_BOT
            speaker = agent_name
        else:
            speaker = f"User({user})"

        lines.append(f"[{speaker}]: {text}")

    return "\n".join(lines)


def build_context(
    role_md: str,
    memory: str,
    thread_history: list[dict],
    system_docs: str,
    bot_user_id: str | None = None,
    agent_name: str = "Lisa",
    soul_md: str = "",
    personality_md: str = "",
) -> str:
    """Assemble a full context string from all available sources.

    Components are assembled in this order:
    1. SOUL (universal behavior rules — shared across all agents)
    2. Role definition (role.md)
    3. Personality (agent-specific voice)
    4. Memory (accumulated knowledge)
    5. System documentation
    6. Thread history (conversation transcript)

    Args:
        role_md: The agent's role definition content.
        memory: Accumulated memory content.
        thread_history: Parsed thread messages.
        system_docs: System/integration documentation.
        bot_user_id: The Slack bot user ID for speaker labeling.
        agent_name: Display name for the agent.
        soul_md: Universal behavior rules (SOUL.md content).
        personality_md: Agent-specific personality content.

    Returns:
        A single context string with all components.
    """
    sections = []

    if soul_md and soul_md.strip():
        sections.append(soul_md.strip())

    if role_md and role_md.strip():
        sections.append(role_md.strip())

    if personality_md and personality_md.strip():
        sections.append(personality_md.strip())

    if memory and memory.strip():
        sections.append(memory.strip())

    if system_docs and system_docs.strip():
        sections.append(system_docs.strip())

    if thread_history:
        transcript = build_conversation_context(
            thread_history,
            bot_user_id=bot_user_id,
            agent_name=agent_name,
        )
        if transcript:
            sections.append("## Conversation History\n" + transcript)

    return "\n\n".join(sections)
