"""Slack notification helpers for the auto-dispatch loop.

Best-effort by design: a missing client/channel or a failed post is logged
and swallowed — notifications must never wedge the loop.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _slack_post(slack_client: Any, channel: str | None, text: str) -> None:
    if slack_client is None or not channel:
        logger.info("auto_dispatch: no Slack client/channel; skipping post: %s", text)
        return
    try:
        await slack_client.chat_postMessage(channel=channel, text=text)
    except Exception:
        logger.exception("auto_dispatch: failed to post notification")


async def _slack_post_with_ts(slack_client: Any, channel: str | None, text: str) -> str:
    """Post a message and return its ``ts`` (empty string if posting fails or no channel)."""
    if slack_client is None or not channel:
        logger.info("auto_dispatch: no Slack client/channel; skipping post: %s", text)
        return ""
    try:
        resp = await slack_client.chat_postMessage(channel=channel, text=text)
        return (resp or {}).get("ts") or ""
    except Exception:
        logger.exception("auto_dispatch: failed to post notification")
        return ""
