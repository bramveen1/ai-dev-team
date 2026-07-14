"""Transport-shared inbound-pipeline helpers (refactoring roadmap §2c, slice 1).

The Slack event path (:mod:`router.slack_events`) and the Discord adapter
carried near-verbatim copies of four inbound stages: event dedup,
attachment-dir mtime refresh, attachment ingest (validate → reject →
ingest → warn → abort, with identical user-facing strings), and
agent-handoff recording. This module owns each stage's logic once.

Each transport keeps thin wrappers so its local names, state stores, and
test patch targets stay stable — in particular, ``validate_fn`` /
``ingest_fn`` / ``store_factory`` are passed in at call time so tests that
patch ``router.slack_events.validate_files`` or
``router.chat.adapters.discord.ingest_files`` keep intercepting. The full
hoist of the ~10-stage inbound orchestration into ``chat/core`` is the rest
of §2c (#553); this slice only removes the copy-paste.
"""

from __future__ import annotations

import collections
import logging
import os
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Dedup-store sizing shared by every transport: entries live for the TTL,
# the store is FIFO-capped so an event storm can't grow it unbounded.
SEEN_EVENTS_MAX: int = 1024
SEEN_EVENTS_TTL: float = 300.0  # seconds


def seen_recently(
    store: collections.OrderedDict,
    key: Any,
    *,
    ttl: float = SEEN_EVENTS_TTL,
    max_size: int = SEEN_EVENTS_MAX,
    now: float | None = None,
) -> bool:
    """TTL + FIFO-capped dedup check; returns True when *key* was seen within *ttl*.

    On a miss (or an expired hit) the key is (re)recorded with a fresh
    expiry and the store is trimmed to *max_size* oldest-first. *store* is
    the caller's own ``OrderedDict`` — module-level for the Slack path,
    per-adapter for Discord — so tests can keep clearing it directly.
    """
    if now is None:
        now = time.monotonic()
    if key in store:
        if store[key] > now:
            return True
        del store[key]
    store[key] = now + ttl
    store.move_to_end(key)
    while len(store) > max_size:
        store.popitem(last=False)
    return False


def bump_attachment_thread_mtime(thread_key: str, *, attachments_root: str) -> None:
    """Touch the per-thread attachments dir to refresh its GC TTL if it exists.

    Called on every real (non-duplicate) event so the attachments janitor
    keeps active threads alive. Does not create the dir — that happens on
    first file ingest (#328/#330).
    """
    if not thread_key:
        return
    thread_dir = os.path.join(attachments_root, thread_key)
    try:
        if os.path.isdir(thread_dir):
            os.utime(thread_dir, None)
    except OSError:
        logger.debug("Failed to bump mtime for attachments thread dir %s", thread_key)


async def ingest_attachments(
    raw_files: list[dict],
    thread_key: str,
    bot_token: str,
    *,
    attachments_root: str,
    agent_name: str,
    post_notice: Callable[[str], Awaitable[None]],
    validate_fn: Callable,
    ingest_fn: Callable,
    require_token: bool,
) -> tuple[list[str], bool]:
    """Validate and ingest inbound files; post user-facing notices via *post_notice*.

    Returns ``(paths, ok)``. ``ok`` is False when the dispatch must be
    aborted (validation rejection or ingest failure) — a user-facing reply
    has already been posted in that case. Ingest failures abort rather than
    continue: a plausible-looking answer that silently ignored the user's
    files is worse than asking for a retry.

    ``require_token=True`` preserves the Slack path's behavior of silently
    skipping ingest (but still validating) when the agent has no bot token
    to download with; Discord CDN URLs are pre-authorised, so the Discord
    path passes False and ingests tokenless.

    ``validate_fn``/``ingest_fn`` are passed from the calling module's
    namespace so its test patch targets keep working.
    """
    valid_files, rejection = validate_fn(raw_files, thread_key, attachments_root=attachments_root)
    if rejection:
        logger.warning("attachments: rejected agent=%s thread=%s reason=%s", agent_name, thread_key, rejection)
        try:
            await post_notice(f":no_entry: Could not ingest attachments: {rejection}")
        except Exception:
            logger.exception("attachments: failed to post rejection reply")
        return [], False

    if not valid_files or (require_token and not bot_token):
        return [], True

    try:
        paths, conversion_warnings = await ingest_fn(
            valid_files, thread_key, bot_token, attachments_root=attachments_root
        )
    except Exception:
        logger.exception("attachments: ingest raised agent=%s thread=%s", agent_name, thread_key)
        try:
            await post_notice(":no_entry: Could not process attachments — please try again.")
        except Exception:
            logger.exception("attachments: failed to post ingest-failure reply")
        return [], False

    for warning in conversion_warnings:
        try:
            await post_notice(f":warning: {warning}")
        except Exception:
            logger.exception("attachments: failed to post conversion warning")
    return paths, True


def record_handoff(
    mentioned: str | None,
    *,
    current_agent: str,
    channel_key: str,
    thread_key: str,
    store_factory: Callable,
) -> None:
    """Persist an agent-initiated handoff when the reply @mentions another agent.

    Callers resolve *mentioned* themselves (mention parsing is
    transport-specific — the Slack path maps bot user IDs, Discord matches
    display names) and pass their module's ``store_factory`` so patched
    stores keep intercepting. No-op when nothing was mentioned, the mention
    is the current agent, or the thread coordinates are missing.
    """
    if not mentioned or mentioned == current_agent:
        return
    if not channel_key or not thread_key:
        return
    try:
        store_factory().set_active_agent(
            channel_id=channel_key,
            thread_ts=thread_key,
            agent_name=mentioned,
            mentioned=True,
        )
        logger.info("Agent handoff detected: %s -> %s in thread=%s", current_agent, mentioned, thread_key)
    except Exception:
        logger.exception("Failed to record agent-initiated handoff")
