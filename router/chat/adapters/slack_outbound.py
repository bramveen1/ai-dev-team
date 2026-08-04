"""Flag-gated plain-text outbound Slack routing through ``ChatAdapter.send_message`` (#801).

Deferred outbound slice of #553: inbound already routes through
:class:`~router.chat.adapters.slack.SlackAdapter` (``SLACK_VIA_ADAPTER``, now
on); everything the router *emits* proactively — scheduled-task output,
approval status/gate-failure notices, attachment-rejection notices, draft
reminders — still called ``chat_postMessage`` directly on an injected client.
This module is the single choke point those call-sites route through once
``SLACK_OUTBOUND_VIA_ADAPTER`` is on; flag off (default), callers keep using
their existing ``client.chat_postMessage`` call, byte-for-byte.

Only plain-text sends live here. Block Kit approval cards, ``chat_update``
edits, and the session-summary metadata marker need a richer
``OutboundMessage`` contract that doesn't exist yet — they stay on the legacy
path unconditionally, flag on or off (see issue #801's out-of-scope
carve-outs).
"""

from __future__ import annotations

from typing import Any

from router import settings
from router.chat.adapters.slack import SlackAdapter, make_outbound_ref
from router.chat.types import OutboundMessage

FLAG = "SLACK_OUTBOUND_VIA_ADAPTER"


def enabled() -> bool:
    """True when ``SLACK_OUTBOUND_VIA_ADAPTER`` is on. Read per-send — hot."""
    return bool(settings.get(FLAG))


async def send(
    client: Any,
    agent_name: str,
    text: str,
    *,
    channel: str = "",
    thread_ts: str = "",
) -> None:
    """Post ``text`` through ``ChatAdapter.send_message``. Call only when :func:`enabled`.

    Mirrors the legacy ``client.chat_postMessage(channel=channel,
    thread_ts=thread_ts or None, text=text)`` call it replaces — same
    destination, with ``md_to_slack`` + outbound mention rewrite now applied
    at the adapter boundary. ``channel`` doubles as the adapter's
    ``default_channel`` so a blank ``thread_ts`` (channel-root post) still
    resolves a ref; an empty ``channel`` fails closed inside
    :meth:`SlackAdapter.send_message`.
    """
    adapter = SlackAdapter(agent_name, client, default_channel=channel)
    ref = make_outbound_ref(channel, thread_ts) if channel else None
    await adapter.send_message(OutboundMessage(text=text, conversation_ref=ref))
