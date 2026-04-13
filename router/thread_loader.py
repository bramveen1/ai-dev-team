"""Thread history loader — fetches and parses Slack thread messages.

Provides functions to load conversation history from Slack threads
and parse raw messages into a structured format for context building.
"""

import logging

logger = logging.getLogger(__name__)

# Message subtypes to filter out (system/meta messages, not real conversation)
_IGNORED_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "group_join",
    "group_leave",
    "bot_add",
    "bot_remove",
}


def parse_thread(messages: list[dict]) -> list[dict]:
    """Parse raw Slack thread messages into a structured list.

    Filters out system messages and extracts user/text/ts fields.
    Messages are returned in chronological order.

    Args:
        messages: Raw Slack message dicts from conversations.replies.

    Returns:
        A list of dicts with keys: user, text, ts.
    """
    parsed = []
    for msg in messages:
        subtype = msg.get("subtype", "")
        if subtype in _IGNORED_SUBTYPES:
            continue

        user = msg.get("user") or msg.get("bot_id", "unknown")
        text = msg.get("text", "")
        ts = msg.get("ts", "")

        if not text.strip():
            continue

        parsed.append({"user": user, "text": text, "ts": ts})

    # Ensure chronological order (sort numerically since Slack ts are floats)
    parsed.sort(key=lambda m: float(m["ts"]) if m["ts"] else 0.0)
    return parsed


def has_summary(messages: list[dict]) -> bool:
    """Detect whether any message in the thread contains a session summary.

    Looks for the "## Session Summary" marker in message text.

    Args:
        messages: List of message dicts (raw or parsed).

    Returns:
        True if a summary marker is found, False otherwise.
    """
    for msg in messages:
        text = msg.get("text", "")
        if "## Session Summary" in text:
            return True
    return False


async def load_thread_history(
    client,
    channel: str,
    thread_ts: str,
    max_messages: int = 20,
) -> list[dict]:
    """Load thread history from Slack using conversations.replies.

    Fetches up to max_messages most recent messages from the thread,
    filters out system messages, and returns parsed message dicts.

    Args:
        client: Slack WebClient instance (async).
        channel: Slack channel ID.
        thread_ts: Thread parent timestamp.
        max_messages: Maximum number of messages to return (most recent).

    Returns:
        A list of dicts with keys: user, text, ts.
        Returns an empty list if the thread has no history or on error.
    """
    try:
        response = await client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=max_messages + 10,  # fetch extra to account for filtered messages
        )
    except Exception:
        logger.exception("Failed to fetch thread history for channel=%s thread_ts=%s", channel, thread_ts)
        return []

    if not response.get("ok"):
        logger.warning("Slack API returned not-ok for conversations_replies")
        return []

    raw_messages = response.get("messages", [])
    parsed = parse_thread(raw_messages)

    # Keep only the most recent max_messages
    if len(parsed) > max_messages:
        parsed = parsed[-max_messages:]

    return parsed
