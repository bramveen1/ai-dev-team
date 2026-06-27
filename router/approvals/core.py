"""Backend-agnostic approval routing.

Provides ``on_approval`` — the backend-neutral seam that handles the core
approval lifecycle (load draft, validate, cleanup external resource for native
discards, transition status).  Slack-specific I/O (ack, message updates,
execute callbacks that carry a Slack client) lives in handlers.py.

Does NOT import from block_kit or slack_sdk.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from router.approvals.store import Draft, DraftStore

logger = logging.getLogger(__name__)

# Type alias for the external draft cleanup callback.
# Receives a Draft and should delete the external resource (e.g. M365 draft).
# Defined here (not in handlers.py) because on_approval invokes it — it is
# backend-agnostic: no Slack client, channel, or ts required.
DraftCleanupCallback = Callable[[Draft], Awaitable[None]]


async def on_approval(
    draft_id: str,
    decision: str,
    store: DraftStore,
    *,
    cleanup_callback: DraftCleanupCallback | None = None,
) -> Draft | None:
    """Backend-agnostic approval routing.

    Loads and validates the draft, optionally cleans up an external resource
    (for native discards), then transitions the draft to the new status.

    Parameters
    ----------
    draft_id  : identifier of the draft being acted on
    decision  : ``"approved"`` or ``"discarded"``
    store     : the DraftStore instance
    cleanup_callback : optional async callback invoked for native drafts before
        the discard transition (e.g. to delete an M365 draft via Graph API).
        Only called when decision == "discarded" and draft.draft_type == "native".

    Returns
    -------
    The updated Draft on success, or None if the draft was not found or was
    already resolved (idempotent — callers can safely no-op on None).
    """
    draft = store.get(draft_id)
    if draft is None:
        logger.warning("on_approval: draft %s not found", draft_id)
        return None

    if draft.status != "pending":
        logger.info("on_approval: draft %s already resolved (status=%s), skipping", draft_id, draft.status)
        return None

    if decision == "discarded" and draft.draft_type == "native" and cleanup_callback is not None:
        try:
            await cleanup_callback(draft)
            logger.info("on_approval: external cleanup succeeded for draft %s", draft_id)
        except Exception:
            logger.exception(
                "on_approval: external cleanup failed for draft %s (will still transition to discarded)", draft_id
            )

    return store.transition(draft_id, decision)
