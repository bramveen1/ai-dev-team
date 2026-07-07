"""Slack notification helpers for the auto-dispatch loop.

Best-effort by design: a missing client/channel or a failed post is logged
and swallowed — notifications must never wedge the loop. The shared
contract lives in :mod:`router.slack_post`; these wrappers keep the
loop-local names and log identity stable.
"""

from __future__ import annotations

import logging
from typing import Any

from router import slack_post

logger = logging.getLogger(__name__)


async def _slack_post(slack_client: Any, channel: str | None, text: str) -> None:
    await slack_post.best_effort_post(slack_client, channel, text, log=logger, prefix="auto_dispatch")


async def _slack_post_with_ts(slack_client: Any, channel: str | None, text: str) -> str:
    """Post a message and return its ``ts`` (empty string if posting fails or no channel)."""
    return await slack_post.best_effort_post(slack_client, channel, text, log=logger, prefix="auto_dispatch")
