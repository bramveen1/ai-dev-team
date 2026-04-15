"""Draft expiration and reminder worker.

Scans for stale drafts and performs three time-based transitions:
1. Pending + past reminder threshold → post thread reminder (idempotent)
2. Pending + past expiration → mark expired, edit Slack message
3. Expired + past cleanup threshold → invoke provider cleanup, mark cleaned_up

Designed to run hourly via the scheduled tasks system.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from router.approvals.store import DraftStore

logger = logging.getLogger(__name__)

# Default TTL configuration
DEFAULT_TTLS: dict[str, str] = {
    "default": "24h",
    "social": "8h",
    "calendar": "72h",
}
DEFAULT_REMINDER_RATIO = 0.5
DEFAULT_CLEANUP_DAYS = 7


def parse_duration(duration_str: str) -> timedelta:
    """Parse a human-readable duration string into a timedelta.

    Supports: '24h', '8h', '72h', '30m', '7d', etc.
    """
    match = re.match(r"^(\d+)\s*([hHmMdD])$", duration_str.strip())
    if not match:
        raise ValueError(f"Invalid duration format: '{duration_str}'. Use e.g. '24h', '30m', '7d'.")

    value = int(match.group(1))
    unit = match.group(2).lower()

    if unit == "h":
        return timedelta(hours=value)
    elif unit == "m":
        return timedelta(minutes=value)
    elif unit == "d":
        return timedelta(days=value)
    else:
        raise ValueError(f"Unknown duration unit: '{unit}'")


def get_ttl(capability_type: str, ttl_config: dict[str, Any] | None = None) -> timedelta:
    """Get the TTL for a capability type from config, falling back to default."""
    config = ttl_config or DEFAULT_TTLS
    duration_str = config.get(capability_type, config.get("default", "24h"))
    if isinstance(duration_str, (int, float)):
        return timedelta(hours=duration_str)
    return parse_duration(str(duration_str))


def get_reminder_offset(capability_type: str, ttl_config: dict[str, Any] | None = None) -> timedelta:
    """Get the reminder offset (time before expiry to send reminder)."""
    config = ttl_config or {}
    ratio = float(config.get("reminder_ratio", DEFAULT_REMINDER_RATIO))
    ttl = get_ttl(capability_type, config)
    return ttl * ratio


def get_cleanup_threshold(ttl_config: dict[str, Any] | None = None) -> timedelta:
    """Get the cleanup threshold (days after expiration to clean up external resources)."""
    config = ttl_config or {}
    days = int(config.get("cleanup_days", DEFAULT_CLEANUP_DAYS))
    return timedelta(days=days)


async def _send_reminder(
    client: Any,
    channel: str,
    message_ts: str,
    capability_type: str,
) -> None:
    """Post a reminder in the thread of the approval message."""
    try:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=message_ts,
            text=f"Hey, still waiting on this {capability_type} draft. Let me know what you'd like to do!",
        )
    except Exception:
        logger.exception("Failed to send reminder for channel=%s ts=%s", channel, message_ts)


async def _expire_draft(
    client: Any,
    channel: str,
    message_ts: str,
    capability_type: str,
    action_verb: str,
) -> None:
    """Edit the Slack message to show the draft has expired."""
    try:
        await client.chat_update(
            channel=channel,
            ts=message_ts,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":clock3: Draft expired — this {capability_type} {action_verb} "
                            "was not acted on in time. Still want to proceed? "
                            "Ask me to recreate it."
                        ),
                    },
                }
            ],
            text=f"Draft expired — {capability_type} {action_verb}",
        )
    except Exception:
        logger.exception("Failed to update expired message for channel=%s ts=%s", channel, message_ts)


async def run_once(
    store: DraftStore,
    client: Any,
    now: datetime | None = None,
    ttl_config: dict[str, Any] | None = None,
    cleanup_callback: Any | None = None,
) -> dict[str, int]:
    """Run one pass of the expiration worker. Idempotent.

    Three phases:
    (a) Pending + past reminder threshold → send reminder, record reminded_at
    (b) Pending + past expires_at → mark expired, edit Slack message
    (c) Expired + past cleanup threshold → invoke cleanup, mark cleaned_up

    Args:
        store: The DraftStore instance.
        client: Slack WebClient for posting messages and updating.
        now: Current time (injectable for testing). Defaults to UTC now.
        ttl_config: TTL configuration dict. Defaults to DEFAULT_TTLS.
        cleanup_callback: Optional async callback(draft) for external resource cleanup.

    Returns:
        Dict with counts: {"reminded": N, "expired": N, "cleaned_up": N}
    """
    if now is None:
        now = datetime.now(timezone.utc)

    config = ttl_config or DEFAULT_TTLS
    counts = {"reminded": 0, "expired": 0, "cleaned_up": 0}

    # Phase A: Send reminders for pending drafts past the reminder threshold
    # We check all pending drafts and compute per-type reminder thresholds
    pending_drafts = store.list_by_status("pending")
    for draft in pending_drafts:
        if draft.reminded_at is not None:
            continue  # Already reminded

        reminder_offset = get_reminder_offset(draft.capability_type, config)
        reminder_time = draft.created_at + reminder_offset

        if now >= reminder_time:
            await _send_reminder(client, draft.slack_channel, draft.slack_message_ts, draft.capability_type)
            store.mark_reminded(draft.draft_id, now)
            counts["reminded"] += 1
            logger.info("Sent reminder for draft %s (type=%s)", draft.draft_id, draft.capability_type)

    # Phase B: Expire pending drafts past their expires_at
    for draft in pending_drafts:
        # Re-fetch to pick up any reminder updates from phase A
        draft = store.get(draft.draft_id)
        if draft is None or draft.status != "pending":
            continue

        if draft.expires_at and now >= draft.expires_at:
            store.transition(draft.draft_id, "expired")
            await _expire_draft(
                client, draft.slack_channel, draft.slack_message_ts, draft.capability_type, draft.action_verb
            )
            counts["expired"] += 1
            logger.info("Expired draft %s (type=%s)", draft.draft_id, draft.capability_type)

    # Phase C: Clean up external resources for expired native-app drafts
    cleanup_delta = get_cleanup_threshold(config)
    cleanup_threshold = now - cleanup_delta
    cleanup_candidates = store.list_expired_needing_cleanup(cleanup_threshold)

    for draft in cleanup_candidates:
        if cleanup_callback:
            try:
                await cleanup_callback(draft)
            except Exception:
                logger.exception("Cleanup callback failed for draft %s", draft.draft_id)
                continue

        store.transition(draft.draft_id, "cleaned_up")
        counts["cleaned_up"] += 1
        logger.info("Cleaned up draft %s (type=%s)", draft.draft_id, draft.capability_type)

    return counts
