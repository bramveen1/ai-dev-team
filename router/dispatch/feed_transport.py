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
through that adapter instead. Flag off, or an unset transport, or a missing
ref — every one of those degrades to the historical
``slack_post.best_effort_post`` call, byte-for-byte. An unresolvable or
unsupported transport skips the post with a clear log line; it never
silently falls back to Slack (that would post into the wrong conversation).

``slack`` itself joins the set of transports with a live ChatAdapter
resolver only when ``SLACK_VIA_ADAPTER`` is also on (#897, mirrors
``router.epic.loop``'s ``_effective_adapter_transports()`` from #875) —
resolving to ``runtime.slack_adapter_for_agent`` instead of the legacy
``slack_post`` call. Default-off, so this is additive: flag off leaves
``slack`` routed through ``slack_post`` exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any

from router import runtime, settings
from router.chat.adapters import slack_post

ENV_FLAG = "DISPATCH_FEED_VIA_CHAT_ADAPTER"
# Mirrors router.epic.loop's SLACK_VIA_ADAPTER flag (#875) — gates whether
# "slack" joins the effective adapter-transport set below.
_SLACK_ADAPTER_ENV_FLAG = "SLACK_VIA_ADAPTER"

# Transports with a live ChatAdapter resolver, before SLACK_VIA_ADAPTER is
# taken into account. Slack is deliberately absent here — see
# _effective_adapter_transports().
_ADAPTER_TRANSPORTS = frozenset({"discord"})

# Transport -> runtime resolver function name, keyed by lookup rather than an
# if/elif chain of transport-string equality checks (mirrors
# router.epic.loop._TRANSPORT_ADAPTER_RESOLVER_NAMES). Looked up via getattr
# at call time so tests can monkeypatch runtime.*_adapter_for_agent.
_TRANSPORT_ADAPTER_RESOLVER_NAMES: dict[str, str] = {
    "discord": "discord_adapter_for_agent",
    "slack": "slack_adapter_for_agent",
}


def is_enabled() -> bool:
    """Return True when the DISPATCH_FEED_VIA_CHAT_ADAPTER setting is truthy (hot-reloadable)."""
    return bool(settings.get(ENV_FLAG))


def _slack_via_adapter_enabled() -> bool:
    """Return True when SLACK_VIA_ADAPTER is truthy (hot-reloadable)."""
    return bool(settings.get(_SLACK_ADAPTER_ENV_FLAG))


def _effective_adapter_transports() -> frozenset[str]:
    """``_ADAPTER_TRANSPORTS`` plus ``slack`` when ``SLACK_VIA_ADAPTER`` is on (#897)."""
    if _slack_via_adapter_enabled():
        return _ADAPTER_TRANSPORTS | {"slack"}
    return _ADAPTER_TRANSPORTS


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

    Flag off, transport unset, or conversation_id missing all degrade to
    ``slack_post.best_effort_post`` — today's exact behaviour (AC:
    missing/unresolvable ref → log-and-skip, no crash).

    "slack" is special-cased: the sidecar ``transport`` file defaults to
    "slack" for *every* dispatch ever launched (handler.py always writes
    it), so unlike "discord" a missing ``conversation_id`` alongside
    transport="slack" is the overwhelmingly common case — the ordinary
    dispatch with no ChatAdapter ref at all — not an error condition worth
    a log-and-skip. That case (and SLACK_VIA_ADAPTER off entirely) degrades
    to the legacy channel/thread_ts ``slack_post`` call exactly as before
    #897. Only a dispatch that actually carries a resolved
    ``"slack:<channel>:<ts>"`` conversation_id (currently: a worker
    dispatched from the epic lane, #897) is eligible for ChatAdapter
    routing, and only when SLACK_VIA_ADAPTER is also on.

    Flag on with a known, effective transport and both a conversation_id
    and a resolvable adapter posts through the adapter.
    """
    effective_transports = _effective_adapter_transports()
    if is_enabled() and transport and transport in effective_transports and conversation_id:
        resolver_name = _TRANSPORT_ADAPTER_RESOLVER_NAMES.get(transport)
        adapter = getattr(runtime, resolver_name)(agent) if resolver_name else None
        if adapter is None:
            log.warning("%s: no %s adapter for agent=%s; skipping post", prefix, transport, agent)
            return

        from router.chat.types import ConversationRef, OutboundMessage

        try:
            await adapter.send_message(OutboundMessage(text=text, conversation_ref=ConversationRef(conversation_id)))
        except Exception:
            log.exception("%s: ChatAdapter post failed agent=%s transport=%s", prefix, agent, transport)
        return

    if (
        is_enabled()
        and transport
        and transport in effective_transports
        and not conversation_id
        and transport != "slack"
    ):
        log.info("%s: missing conversation_id for transport=%s agent=%s; skipping post", prefix, transport, agent)
        return

    if is_enabled() and transport and transport not in effective_transports and transport != "slack":
        log.warning("%s: unsupported transport=%r for agent=%s; skipping post", prefix, transport, agent)
        return

    await slack_post.best_effort_post(slack_client, channel, text, thread_ts=thread_ts or None, log=log, prefix=prefix)
