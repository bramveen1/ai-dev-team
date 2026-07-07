"""Best-effort Slack posting — the shared "never raise" notification helper.

Every background loop (supervision, milestone feed, merge queue,
auto-dispatch, stuck-guard, kill command) needs to post notices without a
Slack failure ever wedging the tick or dispatch it runs in. This module
owns that contract once (refactoring roadmap §8.2); call sites keep their
module-local wrapper names as thin delegates so existing signatures, patch
targets, and log identities stay stable.

This module must stay a leaf: stdlib imports only.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel distinguishing "caller never passes thread_ts" (kwarg omitted from
# the chat_postMessage call, matching the legacy channel-level posters) from
# "caller passes thread_ts, possibly as None" (kwarg always forwarded,
# matching the legacy thread posters). Mock-based tests assert on the exact
# call shape, so kwarg presence must be preserved per call site.
_UNSET: Any = object()

# Strong references to fire-and-forget tasks so they aren't GC'd mid-flight.
_tasks: set[asyncio.Task] = set()


def _resolve_poster(client: Any) -> Any:
    return getattr(client, "chat_postMessage", None) if client is not None else None


def _build_kwargs(channel: str, text: str, thread_ts: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts is not _UNSET:
        kwargs["thread_ts"] = thread_ts
    return kwargs


async def best_effort_post(
    client: Any,
    channel: str | None,
    text: str,
    *,
    thread_ts: Any = _UNSET,
    log: logging.Logger | None = None,
    prefix: str = "slack_post",
) -> str:
    """Post *text* to *channel*; never raise.

    Returns the posted message's ``ts`` (empty string when the post is
    skipped, fails, or the response carries no ``ts``). Handles both async
    and sync ``chat_postMessage`` clients. Missing client/channel is an
    info-level skip, any post failure an exception-level log — both on the
    caller's *log* under *prefix* so log identity stays per-module.
    """
    log = log or logger
    poster = _resolve_poster(client)
    if poster is None or not channel:
        log.info("%s: no Slack client/channel; skipping post: %s", prefix, text)
        return ""
    try:
        result = poster(**_build_kwargs(channel, text, thread_ts))
        if inspect.isawaitable(result):
            result = await result
        return (result or {}).get("ts") or ""
    except Exception:
        log.exception("%s: failed to post to channel=%s", prefix, channel)
        return ""


def fire_and_forget_post(
    client: Any,
    channel: str | None,
    text: str,
    *,
    thread_ts: Any = _UNSET,
    log: logging.Logger | None = None,
    prefix: str = "slack_post",
) -> None:
    """Best-effort post from a synchronous context; never raise or block.

    Sync clients post inline; async clients are scheduled on the running
    loop with a strong task reference held until completion (so the
    coroutine is never GC'd before it sends).
    """
    log = log or logger
    poster = _resolve_poster(client)
    if poster is None or not channel:
        return
    try:
        result = poster(**_build_kwargs(channel, text, thread_ts))
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    except Exception:
        log.exception("%s: failed to post to channel=%s", prefix, channel)
