"""Thread history loader — fetches and parses Slack thread messages.

Provides functions to load conversation history from Slack threads,
parse raw messages into a structured format for context building,
and detect/split session summaries for resume functionality.
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

# Markers used to detect session summary messages posted by the bot on timeout.
# These match the format defined in router/session_end.py SUMMARY_FORMAT.
SUMMARY_MARKERS = [
    "_Session paused",
    "## Session Summary",
]


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

    Looks for summary markers in message text.

    Args:
        messages: List of message dicts (raw or parsed).

    Returns:
        True if a summary marker is found, False otherwise.
    """
    for msg in messages:
        text = msg.get("text", "")
        if any(marker in text for marker in SUMMARY_MARKERS):
            return True
    return False


def find_session_summary(messages: list[dict], bot_user_id: str | None = None) -> str | None:
    """Find the most recent session summary in a thread.

    Scans messages for the most recent one that contains a session summary
    marker. Optionally filters to only messages from a specific bot user.

    Args:
        messages: List of message dicts.
        bot_user_id: Optional bot user ID to filter by.

    Returns:
        The summary text of the most recent summary message, or None.
    """
    if not messages:
        return None

    sorted_msgs = sorted(messages, key=lambda m: m.get("ts", "0"), reverse=True)

    for msg in sorted_msgs:
        text = msg.get("text", "")
        if bot_user_id and msg.get("user") != bot_user_id:
            continue
        if any(marker in text for marker in SUMMARY_MARKERS):
            logger.info("Found session summary in thread (ts=%s)", msg.get("ts"))
            return text

    return None


def split_messages_at_summary(
    messages: list[dict],
    bot_user_id: str | None = None,
) -> tuple[str | None, list[dict]]:
    """Split thread messages into a summary and messages after it.

    Finds the most recent session summary, returns it along with only
    the messages that came after it in the thread. Messages before the
    summary are dropped (the summary captures them).

    Args:
        messages: List of message dicts.
        bot_user_id: Optional bot user ID to filter summaries by.

    Returns:
        A tuple of (summary_text, recent_messages). If no summary is found,
        returns (None, all_messages).
    """
    if not messages:
        return None, []

    sorted_msgs = sorted(messages, key=lambda m: m.get("ts", "0"))

    summary_idx = None
    summary_text = None

    for i, msg in enumerate(sorted_msgs):
        text = msg.get("text", "")
        if bot_user_id and msg.get("user") != bot_user_id:
            continue
        if any(marker in text for marker in SUMMARY_MARKERS):
            summary_idx = i
            summary_text = text

    if summary_idx is None:
        return None, sorted_msgs

    recent = sorted_msgs[summary_idx + 1 :]
    logger.info(
        "Split thread at summary (idx=%d): %d messages before, %d after",
        summary_idx,
        summary_idx,
        len(recent),
    )
    return summary_text, recent


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
