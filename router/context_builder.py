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

# Default token budget warning threshold for build_full_context
DEFAULT_TOKEN_BUDGET = 8000


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
        "[User]: Can you check my calendar?\\n[Lisa]: You have 3 meetings..."
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


def build_full_context(
    memory: dict,
    thread_history: list[dict],
    new_message: str,
    agent_name: str = "",
    session_summary: str | None = None,
    max_tokens: int = DEFAULT_TOKEN_BUDGET,
) -> str:
    """Build the full context for a Claude Code CLI invocation.

    Assembles organizational memory, agent memory, system docs, thread
    history, and the new message. Warns if over budget and truncates
    thread history first, then system docs.

    Args:
        memory: Dict from load_agent_memory() with keys: org_memory,
            agent_memory, system_docs.
        thread_history: List of thread message dicts.
        new_message: The user's latest message.
        agent_name: Display name of the agent (for section headers).
        session_summary: Optional session summary from a previous timeout.
        max_tokens: Maximum token budget for the assembled context.

    Returns:
        The assembled context string, truncated if necessary.
    """
    display_name = agent_name.upper() if agent_name else "AGENT"

    sections = []

    # Organizational memory
    org_memory = memory.get("org_memory", "")
    if org_memory:
        sections.append(f"--- ORGANIZATIONAL MEMORY ---\n{org_memory}")

    # Agent-specific memory
    agent_memory = memory.get("agent_memory", "")
    if agent_memory:
        sections.append(f"--- YOUR MEMORY ({display_name}) ---\n{agent_memory}")

    # System documentation
    system_docs_list = memory.get("system_docs", [])
    system_docs_text = "\n\n".join(system_docs_list) if system_docs_list else ""
    if system_docs_text:
        sections.append(f"--- TOOL DOCUMENTATION ---\n{system_docs_text}")

    # Session summary (for resume from timeout)
    if session_summary:
        sections.append(f"--- PREVIOUS SESSION SUMMARY ---\n{session_summary}")

    # Thread history
    thread_text = build_conversation_context(thread_history, agent_name=agent_name)
    if session_summary and thread_text:
        sections.append(f"--- RECENT MESSAGES (since summary) ---\n{thread_text}")
    elif thread_text:
        sections.append(f"--- CONVERSATION HISTORY ---\n{thread_text}")

    # New message
    if new_message:
        sections.append(f"--- NEW MESSAGE ---\n{new_message}")

    full_context = "\n\n".join(sections)

    # Check token budget
    token_count = estimate_tokens(full_context)
    if token_count > max_tokens:
        logger.warning(
            "Context exceeds token budget: %d tokens > %d max. Truncating.",
            token_count,
            max_tokens,
        )
        full_context = _truncate_context(
            sections=sections,
            max_tokens=max_tokens,
        )

    logger.info("Built full context: %d chars, ~%d tokens", len(full_context), estimate_tokens(full_context))
    return full_context


def _truncate_context(
    sections: list[str],
    max_tokens: int,
) -> str:
    """Truncate context to fit within token budget.

    Strategy: remove thread history first, then system docs.
    """
    # First try: drop thread history
    reduced = [s for s in sections if "CONVERSATION HISTORY" not in s and "RECENT MESSAGES" not in s]
    candidate = "\n\n".join(reduced)
    if estimate_tokens(candidate) <= max_tokens:
        logger.info("Fit within budget by truncating thread history")
        return candidate

    # Second try: also drop system docs
    reduced = [s for s in reduced if "TOOL DOCUMENTATION" not in s]
    candidate = "\n\n".join(reduced)
    if estimate_tokens(candidate) <= max_tokens:
        logger.info("Fit within budget by truncating thread history and system docs")
        return candidate

    # Last resort: hard truncate
    return truncate_to_budget("\n\n".join(reduced), max_tokens)
