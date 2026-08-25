"""Context builder — assembles and manages context for agent dispatch.

Builds a full context string from role definitions, memory, thread history,
and system documentation. Provides token estimation and truncation to stay
within budget limits.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class SectionKind(enum.Enum):
    """Structured identity for a context section.

    ``_truncate_context`` decides what to drop by matching on ``kind`` —
    never by scanning a section's rendered text for header-like substrings.
    Rendered content (e.g. a pasted transcript, or a user message that
    literally contains the words "conversation history") can never be
    mistaken for a different section's kind.
    """

    ORG_MEMORY = "org_memory"
    AGENT_MEMORY = "agent_memory"
    RETRIEVED_MEMORY = "retrieved_memory"
    MEMORY_DIRECTORY = "memory_directory"
    SESSION_SUMMARY = "session_summary"
    THREAD_HISTORY = "thread_history"
    NEW_MESSAGE = "new_message"


# Section kinds dropped (in order) when context exceeds the token budget.
# NEW_MESSAGE is deliberately never included here — the live user message
# must never be droppable, regardless of what text it contains.
_MEMORY_KINDS = (SectionKind.ORG_MEMORY, SectionKind.AGENT_MEMORY, SectionKind.RETRIEVED_MEMORY)
_THREAD_KINDS = (SectionKind.THREAD_HISTORY,)


@dataclass(frozen=True)
class Section:
    """A single rendered context section, tagged with its structured kind."""

    kind: SectionKind
    text: str


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
    agent_name: str = "Agent",
    bot_user_map: dict[str, str] | None = None,
) -> str:
    """Format thread history as a readable conversation transcript.

    When a thread involves multiple agents (after handoffs), ``bot_user_map``
    maps each known bot user ID to its agent display name so every agent
    message is labelled with the correct speaker. Messages from the primary
    bot (``bot_user_id``) and messages stamped with an agent name in the
    ``user`` field (as the router does when it logs its own responses) are
    also labelled with the agent display name.

    Args:
        thread_history: Parsed thread messages (list of dicts with user/text/ts).
        bot_user_id: The primary Slack bot user ID, used as the fallback
            identifier for the current agent.
        agent_name: Display name for the receiving agent (default: "Lisa").
            Used as the label for the primary bot and as a fallback.
        bot_user_map: Optional mapping of Slack bot user IDs to agent
            display names.

    Returns:
        A formatted transcript string, e.g.:
        ``[User(U001)]: Hey\\n[Lisa]: Hi\\n[Sam]: I can weigh in...``
    """
    if not thread_history:
        return ""

    bot_user_map = bot_user_map or {}
    # Normalise map keys and values so lookups are case-insensitive but
    # display values preserve their original casing.
    known_agent_values = {v.lower(): v for v in bot_user_map.values()}

    lines = []
    for msg in thread_history:
        user = msg.get("user", "unknown")
        text = msg.get("text", "")

        if user in bot_user_map:
            speaker = bot_user_map[user]
        elif user.lower() in known_agent_values:
            # thread_history can be synthesised with agent-name keys
            # (e.g. the router's own session log), preserve the mapped casing.
            speaker = known_agent_values[user.lower()]
        elif bot_user_id and user == bot_user_id:
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
    agent_name: str = "Agent",
    worldview_md: str = "",
    personality_md: str = "",
) -> str:
    """Assemble a full context string from all available sources.

    Components are assembled in this order:
    1. WORLDVIEW (universal behavior rules — shared across all agents)
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
        worldview_md: Universal behavior rules (WORLDVIEW.md content).
        personality_md: Agent-specific personality content.

    Returns:
        A single context string with all components.
    """
    sections = []

    if worldview_md and worldview_md.strip():
        sections.append(worldview_md.strip())

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
    bot_user_map: dict[str, str] | None = None,
) -> str:
    """Build the full context for a Claude Code CLI invocation.

    Assembles organizational memory, agent memory, thread history, and the
    new message. Warns if over budget and truncates thread history if so.

    Args:
        memory: Dict from load_agent_memory() with keys: org_memory,
            agent_memory, and optionally retrieved_memory (list of
            (relative path, content) tuples from the retriever).
        thread_history: List of thread message dicts.
        new_message: The user's latest message.
        agent_name: Display name of the agent (for section headers).
        session_summary: Optional session summary from a previous timeout.
        max_tokens: Maximum token budget for the assembled context.

    Returns:
        The assembled context string, truncated if necessary.
    """
    display_name = agent_name.upper() if agent_name else "AGENT"

    sections: list[Section] = []

    # Organizational memory
    org_memory = memory.get("org_memory", "")
    if org_memory:
        sections.append(Section(SectionKind.ORG_MEMORY, f"--- ORGANIZATIONAL MEMORY ---\n{org_memory}"))

    # Agent-specific memory
    agent_memory = memory.get("agent_memory", "")
    if agent_memory:
        sections.append(Section(SectionKind.AGENT_MEMORY, f"--- YOUR MEMORY ({display_name}) ---\n{agent_memory}"))

    # Relevant structured memory selected by the retriever (issue #640).
    retrieved = memory.get("retrieved_memory") or []
    if retrieved:
        parts = [f"### {rel_path}\n{content.strip()}" for rel_path, content in retrieved]
        sections.append(
            Section(SectionKind.RETRIEVED_MEMORY, "--- RELEVANT LONG-TERM MEMORY ---\n" + "\n\n".join(parts))
        )

    # Memory directory path for on-demand long-term memory retrieval
    agent_key = agent_name.lower() if agent_name else "agent"
    sections.append(
        Section(
            SectionKind.MEMORY_DIRECTORY,
            f"--- MEMORY DIRECTORY ---\nYour long-term memory: /config/agents/{agent_key}/memory/",
        )
    )

    # Session summary (for resume from timeout)
    if session_summary:
        sections.append(Section(SectionKind.SESSION_SUMMARY, f"--- PREVIOUS SESSION SUMMARY ---\n{session_summary}"))

    # Thread history
    thread_text = build_conversation_context(
        thread_history,
        agent_name=agent_name,
        bot_user_map=bot_user_map,
    )
    if session_summary and thread_text:
        sections.append(Section(SectionKind.THREAD_HISTORY, f"--- RECENT MESSAGES (since summary) ---\n{thread_text}"))
    elif thread_text:
        sections.append(Section(SectionKind.THREAD_HISTORY, f"--- CONVERSATION HISTORY ---\n{thread_text}"))

    # New message
    if new_message:
        sections.append(Section(SectionKind.NEW_MESSAGE, f"--- NEW MESSAGE ---\n{new_message}"))

    full_context = "\n\n".join(s.text for s in sections)

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
    sections: list[Section],
    max_tokens: int,
) -> str:
    """Truncate context to fit within token budget.

    Sections are dropped by matching their structured ``kind`` tag — never
    by scanning rendered text for header-like substrings. This guarantees a
    section's fate can't be influenced by its own (or another section's)
    content; in particular ``SectionKind.NEW_MESSAGE`` is never included in
    either drop stage below, so the live user message always survives.

    Strategy (in order):
      1. Drop org and agent memory sections — losing stale memory is almost
         always preferable to losing the live conversation.
      2. If still over budget, drop thread history sections
         (``CONVERSATION HISTORY`` / ``RECENT MESSAGES``).
      3. If still over budget, fall back to a hard tail truncation.
    """
    reduced = [s for s in sections if s.kind not in _MEMORY_KINDS]
    if len(reduced) != len(sections):
        logger.info("Dropped memory sections to fit within token budget")
    candidate = "\n\n".join(s.text for s in reduced)
    if estimate_tokens(candidate) <= max_tokens:
        return candidate

    reduced_no_thread = [s for s in reduced if s.kind not in _THREAD_KINDS]
    if len(reduced_no_thread) != len(reduced):
        logger.info("Dropped thread history sections to fit within token budget")
    candidate = "\n\n".join(s.text for s in reduced_no_thread)
    if estimate_tokens(candidate) <= max_tokens:
        return candidate

    return truncate_to_budget(candidate, max_tokens)
