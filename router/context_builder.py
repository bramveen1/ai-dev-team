"""Router context builder — assembles full context for Claude Code CLI invocations.

Combines role definitions, memory, thread history, and system documentation
into a single context string. Provides token estimation and truncation
to stay within budget.
"""

import logging

logger = logging.getLogger(__name__)

# Approximate tokens-per-character ratio (conservative estimate for English text).
# ~4 characters per token on average; we use 0.25 tokens/char.
TOKENS_PER_CHAR = 0.25

# Default token budget warning threshold
DEFAULT_TOKEN_BUDGET = 8000


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text string.

    Uses a simple character-based heuristic (~4 chars per token).

    Args:
        text: The text to estimate.

    Returns:
        Estimated token count as an integer.
    """
    if not text:
        return 0
    return int(len(text) * TOKENS_PER_CHAR)


def truncate_to_budget(text: str, max_tokens: int) -> str:
    """Truncate text to fit within a token budget.

    Args:
        text: The text to truncate.
        max_tokens: Maximum number of tokens allowed.

    Returns:
        The original text if within budget, or a truncated version.
    """
    if estimate_tokens(text) <= max_tokens:
        return text

    # Convert token budget back to approximate character limit
    max_chars = int(max_tokens / TOKENS_PER_CHAR)
    truncated = text[:max_chars]

    # Try to break at a newline for cleaner output
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]

    logger.warning(
        "Truncated context from %d to %d tokens (approx %d chars)",
        estimate_tokens(text),
        estimate_tokens(truncated),
        len(truncated),
    )
    return truncated


def _format_thread_history(thread_history: list[dict]) -> str:
    """Format thread history messages into a readable string.

    Args:
        thread_history: List of message dicts with 'user' and 'text' keys.

    Returns:
        Formatted thread history string.
    """
    if not thread_history:
        return ""

    lines = []
    for msg in thread_history:
        user = msg.get("user", "unknown")
        text = msg.get("text", "")
        lines.append(f"[{user}]: {text}")
    return "\n".join(lines)


def build_context(
    role_md: str,
    memory: str,
    thread_history: list[dict],
    system_docs: str,
    new_message: str = "",
) -> str:
    """Assemble a full context string from components.

    The context is assembled in a fixed order:
    1. Role definition
    2. Memory (org + agent combined)
    3. System documentation
    4. Thread history
    5. New message

    Args:
        role_md: Agent role definition markdown.
        memory: Combined memory content (org + agent).
        thread_history: List of thread message dicts.
        system_docs: Combined system documentation content.
        new_message: The latest user message (optional, can be in thread_history).

    Returns:
        The assembled context string.
    """
    sections = []

    if role_md:
        sections.append(f"--- ROLE ---\n{role_md}")

    if memory:
        sections.append(f"--- MEMORY ---\n{memory}")

    if system_docs:
        sections.append(f"--- TOOL DOCUMENTATION ---\n{system_docs}")

    thread_text = _format_thread_history(thread_history)
    if thread_text:
        sections.append(f"--- CONVERSATION HISTORY ---\n{thread_text}")

    if new_message:
        sections.append(f"--- NEW MESSAGE ---\n{new_message}")

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
    thread_text = _format_thread_history(thread_history)
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
        # Truncate thread history first by rebuilding without it
        full_context = _truncate_context(
            sections=sections,
            thread_text=thread_text,
            system_docs_text=system_docs_text,
            max_tokens=max_tokens,
            session_summary=session_summary,
        )

    logger.info("Built full context: %d chars, ~%d tokens", len(full_context), estimate_tokens(full_context))
    return full_context


def _truncate_context(
    sections: list[str],
    thread_text: str,
    system_docs_text: str,
    max_tokens: int,
    session_summary: str | None = None,
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
