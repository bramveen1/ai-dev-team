"""Slack notification helpers for the auto-dispatch loop.

Best-effort by design: a missing client/channel or a failed post is logged
and swallowed — notifications must never wedge the loop. The shared
contract lives in :mod:`router.slack_post`; these wrappers keep the
loop-local names and log identity stable.

Behind the default-off ``AUTO_DISPATCH_NOTIFY_VIA_CHAT_ADAPTER`` flag (#827,
mirrors ``router.dispatch.feed_transport``'s #713 pattern), a notice carrying
a resolvable non-Slack ``transport``/``conversation_id`` posts through that
ChatAdapter instead. Flag off, a missing/Slack transport, or a missing
``conversation_id`` (i.e. every existing call site, none of which pass these
yet) all degrade to the historical ``slack_post.best_effort_post`` call,
byte-for-byte — including the returned ``ts``. An unresolvable or unsupported
transport skips the post with a clear log line; it never silently falls back
to Slack (that would post into the wrong conversation). The adapter has no
``ts`` concept, so the adapter path surfaces ``conversation_id`` itself as the
edit/ref handle on success.
"""

from __future__ import annotations

import logging
from typing import Any

from router import runtime, settings, slack_post

logger = logging.getLogger(__name__)

ENV_FLAG = "AUTO_DISPATCH_NOTIFY_VIA_CHAT_ADAPTER"

# Transports with a live ChatAdapter resolver. Slack is deliberately absent —
# the Slack path always goes through the legacy slack_post call below.
_ADAPTER_TRANSPORTS = frozenset({"discord"})


def _adapter_enabled() -> bool:
    """Return True when AUTO_DISPATCH_NOTIFY_VIA_CHAT_ADAPTER is truthy (hot-reloadable)."""
    return bool(settings.get(ENV_FLAG))


async def _post_via_chat_adapter(*, agent: str, transport: str, conversation_id: str, text: str) -> bool:
    from router.chat.types import ConversationRef, OutboundMessage

    adapter = runtime.discord_adapter_for_agent(agent)
    if adapter is None:
        logger.warning("auto_dispatch: no %s adapter for agent=%s; skipping post", transport, agent)
        return False
    try:
        await adapter.send_message(OutboundMessage(text=text, conversation_ref=ConversationRef(conversation_id)))
    except Exception:
        logger.exception("auto_dispatch: ChatAdapter post failed agent=%s transport=%s", agent, transport)
        return False
    return True


async def _post(
    slack_client: Any,
    channel: str | None,
    text: str,
    *,
    agent: str = "",
    transport: str = "",
    conversation_id: str = "",
) -> str:
    """Post *text* via the best-available transport; never raises.

    Returns the message's ``ts`` (Slack path) or ``conversation_id`` (adapter
    path) as an edit/ref handle, empty string when the post is skipped or
    fails.
    """
    if _adapter_enabled() and transport and transport != "slack":
        if not conversation_id:
            logger.info(
                "auto_dispatch: missing conversation_id for transport=%s agent=%s; skipping post", transport, agent
            )
            return ""
        if transport not in _ADAPTER_TRANSPORTS:
            logger.warning("auto_dispatch: unsupported transport=%r for agent=%s; skipping post", transport, agent)
            return ""
        sent = await _post_via_chat_adapter(
            agent=agent, transport=transport, conversation_id=conversation_id, text=text
        )
        return conversation_id if sent else ""

    return await slack_post.best_effort_post(slack_client, channel, text, log=logger, prefix="auto_dispatch")


async def _slack_post(
    slack_client: Any,
    channel: str | None,
    text: str,
    *,
    agent: str = "",
    transport: str = "",
    conversation_id: str = "",
) -> None:
    await _post(slack_client, channel, text, agent=agent, transport=transport, conversation_id=conversation_id)


async def _slack_post_with_ts(
    slack_client: Any,
    channel: str | None,
    text: str,
    *,
    agent: str = "",
    transport: str = "",
    conversation_id: str = "",
) -> str:
    """Post a message and return its ``ts`` (empty string if posting fails or no channel)."""
    return await _post(slack_client, channel, text, agent=agent, transport=transport, conversation_id=conversation_id)
