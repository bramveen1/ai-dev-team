"""Shared ChatAdapter routing for the milestone_feed / supervision posters (#713).

``milestone_feed`` and ``supervision`` are the router-side progress feed:
they run every supervision tick and post short status lines into the
originating conversation. Historically that meant a hard call to
``slack_post.best_effort_post`` — fine on Slack, but a dispatch launched
from Discord (or any non-Slack transport) has no Slack client/channel, so
every line was silently dropped ("no Slack client/channel; skipping post").

This module is the single choke point both posters route through. Behind
the default-off ``DISPATCH_FEED_VIA_CHAT_ADAPTER`` flag (mirrors #707's
``DISCORD_WORKER_STATUS_VIA_AGENT``), a dispatch whose persisted
``transport``/``conversation_id`` resolve to a known ``ChatAdapter`` posts
through that adapter instead. Flag off, or a Slack/unset transport, or a
missing ref — every one of those degrades to the historical
``slack_post.best_effort_post`` call, byte-for-byte. An unresolvable or
unsupported transport skips the post with a clear log line; it never
silently falls back to Slack (that would post into the wrong conversation).
"""

from __future__ import annotations

import logging
from typing import Any

from router import runtime, settings, slack_post

ENV_FLAG = "DISPATCH_FEED_VIA_CHAT_ADAPTER"

# Transports with a live ChatAdapter resolver. Slack is deliberately absent —
# the Slack path always goes through the legacy slack_post call below.
_ADAPTER_TRANSPORTS = frozenset({"discord"})


def is_enabled() -> bool:
    """Return True when the DISPATCH_FEED_VIA_CHAT_ADAPTER setting is truthy (hot-reloadable)."""
    return bool(settings.get(ENV_FLAG))


async def post(
    *,
    slack_client: Any,
    channel: str,
    thread_ts: str,
    text: str,
    agent: str,
    transport: str,
    conversation_id: str,
    log: logging.Logger,
    prefix: str,
) -> None:
    """Post *text* via the best-available transport for this dispatch. Never raises.

    Flag off, transport unset/"slack", or conversation_id missing all
    degrade to ``slack_post.best_effort_post`` — today's exact behaviour
    (AC: missing/unresolvable ref → log-and-skip, no crash). Flag on with a
    known non-Slack transport and a resolvable adapter posts through it.
    """
    if is_enabled() and transport and transport != "slack":
        if not conversation_id:
            log.info("%s: missing conversation_id for transport=%s agent=%s; skipping post", prefix, transport, agent)
            return
        if transport not in _ADAPTER_TRANSPORTS:
            log.warning("%s: unsupported transport=%r for agent=%s; skipping post", prefix, transport, agent)
            return
        adapter = runtime.discord_adapter_for_agent(agent)
        if adapter is None:
            log.warning("%s: no %s adapter for agent=%s; skipping post", prefix, transport, agent)
            return

        from router.chat.types import ConversationRef, OutboundMessage

        try:
            await adapter.send_message(OutboundMessage(text=text, conversation_ref=ConversationRef(conversation_id)))
        except Exception:
            log.exception("%s: ChatAdapter post failed agent=%s transport=%s", prefix, agent, transport)
        return

    await slack_post.best_effort_post(slack_client, channel, text, thread_ts=thread_ts or None, log=log, prefix=prefix)
