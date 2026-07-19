"""Pending-reply futures for push-based scripted input collection.

Some transports deliver inbound messages as events (Slack's Bolt handlers)
rather than exposing a pollable history that updates fast enough to drive
:func:`router.chat.input_collect.collect_input_scripted`'s ``read_thread``
loop. For those transports, a scripted collector registers a future here
keyed by the conversation's ref string; the transport's inbound event
handler calls :func:`resolve_reply` with the user's next message in that
conversation, which both delivers the reply to the collector *and* tells
the event handler to consume the message (skip normal dispatch) — exactly
the contract the legacy ``router.packs.grants.SlackPrompt`` registry had.

Keys are the opaque ``ConversationRef`` strings; this module never parses
them, so it stays transport-neutral. One pending reply per conversation:
scripted forms are strictly sequential.
"""

from __future__ import annotations

import asyncio

_pending: dict[str, asyncio.Future[str]] = {}


async def wait_for_reply(conversation_key: str, timeout_seconds: float) -> str | None:
    """Await the next inbound message for ``conversation_key``.

    Returns the message text, or ``None`` when no reply arrives within
    ``timeout_seconds``. The registration is always cleaned up on exit.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _pending[conversation_key] = future
    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending.pop(conversation_key, None)


def resolve_reply(conversation_key: str, text: str) -> bool:
    """Deliver ``text`` to a collector awaiting a reply on ``conversation_key``.

    Returns ``True`` when the reply was consumed — the caller must then stop
    normal processing of the message (it was an answer to a pending prompt,
    not a new instruction). Returns ``False`` when nothing is waiting.
    """
    future = _pending.get(conversation_key)
    if future is None or future.done():
        return False
    future.set_result(text)
    return True


def has_pending(conversation_key: str) -> bool:
    """True when a collector is currently awaiting a reply on this conversation."""
    future = _pending.get(conversation_key)
    return future is not None and not future.done()
