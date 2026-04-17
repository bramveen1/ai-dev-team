"""Agent mention parsing — detect @agent mentions in Slack text.

Two mention formats are recognised:

* Slack user-id format ``<@U12345>`` — resolved against a ``bot_user_map``
  that maps Slack bot user IDs to logical agent names. This is how humans
  @-mention a bot in Slack.
* Plain-name format ``@agent`` — used by agents when they hand off to
  another agent in their own response text, because agents don't know the
  other agents' Slack user IDs.
"""

from __future__ import annotations

import re

# Matches Slack user-id mentions, e.g. ``<@U01234ABC>`` or ``<@U01234ABC|lisa>``.
# Production Slack IDs are alphanumeric, but tests use underscore-separated IDs
# like ``U_BOT_LISA``, so the character class accepts both.
_USER_MENTION_RE = re.compile(r"<@([A-Za-z0-9_]+)(?:\|[^>]+)?>")

# Matches plain-name @mentions. Must be preceded by start-of-string or a
# non-word char so e.g. ``email@example.com`` doesn't match. Word boundary
# at the end so ``@samuel`` doesn't match ``sam``.
_NAME_MENTION_RE = re.compile(r"(?:^|(?<=\W))@([A-Za-z][A-Za-z0-9_-]*)\b")


def parse_mentions(
    text: str,
    agent_names: list[str],
    bot_user_map: dict[str, str] | None = None,
) -> list[str]:
    """Return the agent names mentioned in ``text`` in the order they appear.

    Duplicate mentions are preserved (callers can de-duplicate if they want,
    but order-of-appearance matters for "last mention wins" logic).

    Args:
        text: Message text that may contain mentions.
        agent_names: The logical agent names known to the router. Matching
            is case-insensitive.
        bot_user_map: Optional mapping of Slack bot user IDs to logical
            agent names, used to resolve ``<@U…>`` style mentions.

    Returns:
        A list of logical agent names (lower-case) in order of appearance.
    """
    if not text or not agent_names:
        return []

    bot_user_map = bot_user_map or {}
    known = {name.lower() for name in agent_names}

    matches: list[tuple[int, str]] = []

    for m in _USER_MENTION_RE.finditer(text):
        user_id = m.group(1)
        agent = bot_user_map.get(user_id)
        if agent and agent.lower() in known:
            matches.append((m.start(), agent.lower()))

    for m in _NAME_MENTION_RE.finditer(text):
        name = m.group(1).lower()
        if name in known:
            matches.append((m.start(), name))

    matches.sort(key=lambda pair: pair[0])
    return [name for _, name in matches]


def last_mentioned(
    text: str,
    agent_names: list[str],
    bot_user_map: dict[str, str] | None = None,
) -> str | None:
    """Return the last agent mentioned in ``text``, or None if none."""
    mentions = parse_mentions(text, agent_names, bot_user_map)
    return mentions[-1] if mentions else None


def resolve_target_agent(
    text: str,
    agent_names: list[str],
    bot_user_map: dict[str, str] | None = None,
    active_agent: str | None = None,
    default_agent: str | None = None,
) -> tuple[str | None, bool]:
    """Pick the agent that should handle this message.

    Resolution order:

    1. If ``text`` contains an @mention of a known agent, pick the last
       mentioned agent (``mentioned=True``).
    2. Otherwise, if ``active_agent`` is set (the thread already has an
       active agent), pick it (``mentioned=False``).
    3. Otherwise fall back to ``default_agent`` (``mentioned=False``).

    Args:
        text: Message text.
        agent_names: Known logical agent names.
        bot_user_map: Slack bot-user-id -> agent name mapping.
        active_agent: Last-known active agent for the thread, if any.
        default_agent: Fallback when no mention and no active agent.

    Returns:
        ``(agent_name, was_mentioned)``. ``agent_name`` may be ``None`` if
        nothing resolves (no mentions, no active agent, no default).
    """
    mentioned = last_mentioned(text, agent_names, bot_user_map)
    if mentioned is not None:
        return mentioned, True

    if active_agent and active_agent.lower() in {n.lower() for n in agent_names}:
        return active_agent.lower(), False

    if default_agent and default_agent.lower() in {n.lower() for n in agent_names}:
        return default_agent.lower(), False

    return None, False
