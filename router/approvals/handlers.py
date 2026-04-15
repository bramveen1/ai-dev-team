"""Slack interactivity handlers for approval flow actions.

Each handler follows the pattern:
1. ack() immediately (meet Slack's 3-second requirement)
2. Load the draft from the store
3. Transition the draft status
4. Edit the Slack message to show the outcome
5. Dispatch to the owning agent for execution (approve) or cleanup (discard)

Handlers are registered with a slack_bolt AsyncApp via register_handlers().
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_REQUEST_EDIT,
    build_outcome_message,
)
from router.approvals.store import DraftStore

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

# Module-level store reference, set by register_handlers()
_store: DraftStore | None = None


def _get_store() -> DraftStore:
    """Return the module-level store, raising if not initialized."""
    if _store is None:
        raise RuntimeError("Approval handlers not registered — call register_handlers() first")
    return _store


async def _handle_approve(ack: Any, body: dict, client: Any, action_id: str) -> None:
    """Common handler for all approve actions (send, publish, book)."""
    await ack()

    store = _get_store()
    draft_id = body["actions"][0]["value"]

    logger.info("Approval action=%s draft_id=%s", action_id, draft_id)

    draft = store.get(draft_id)
    if draft is None:
        logger.warning("Draft %s not found for action %s", draft_id, action_id)
        return

    if draft.status != "pending":
        logger.info("Draft %s already resolved (status=%s), skipping", draft_id, draft.status)
        return

    # Transition to approved
    draft = store.transition(draft_id, "approved")

    # Edit the Slack message to show outcome
    outcome = build_outcome_message(draft, approved=True)
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    try:
        await client.chat_update(
            channel=channel,
            ts=message_ts,
            blocks=outcome["blocks"],
            text=f"Approved: {draft.action_verb}",
        )
    except Exception:
        logger.exception("Failed to update Slack message for draft %s", draft_id)

    logger.info("Draft %s approved and message updated", draft_id)


async def _handle_discard(ack: Any, body: dict, client: Any) -> None:
    """Handle the discard action — mark draft as discarded and update message."""
    await ack()

    store = _get_store()
    draft_id = body["actions"][0]["value"]

    logger.info("Discard action draft_id=%s", draft_id)

    draft = store.get(draft_id)
    if draft is None:
        logger.warning("Draft %s not found for discard", draft_id)
        return

    if draft.status != "pending":
        logger.info("Draft %s already resolved (status=%s), skipping", draft_id, draft.status)
        return

    # Transition to discarded
    draft = store.transition(draft_id, "discarded")

    # Edit the Slack message to show outcome
    outcome = build_outcome_message(draft, approved=False)
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    try:
        await client.chat_update(
            channel=channel,
            ts=message_ts,
            blocks=outcome["blocks"],
            text="Discarded",
        )
    except Exception:
        logger.exception("Failed to update Slack message for draft %s", draft_id)

    logger.info("Draft %s discarded and message updated", draft_id)


async def _handle_request_edit(ack: Any, body: dict, client: Any) -> None:
    """Handle the edit request — post a thread reply asking for clarification."""
    await ack()

    store = _get_store()
    draft_id = body["actions"][0]["value"]

    logger.info("Edit request for draft_id=%s", draft_id)

    draft = store.get(draft_id)
    if draft is None:
        logger.warning("Draft %s not found for edit request", draft_id)
        return

    if draft.status != "pending":
        logger.info("Draft %s already resolved (status=%s), skipping", draft_id, draft.status)
        return

    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    try:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=message_ts,
            text=f"What changes would you like to make to this {draft.capability_type} draft?",
        )
    except Exception:
        logger.exception("Failed to post edit request thread for draft %s", draft_id)


def register_handlers(bolt_app: AsyncApp, store: DraftStore) -> None:
    """Register all approval action handlers with the Slack bolt app.

    Args:
        bolt_app: The slack_bolt AsyncApp instance.
        store: The DraftStore instance for persisting draft state.
    """
    global _store
    _store = store

    @bolt_app.action(ACTION_APPROVE_SEND)
    async def handle_approve_send(ack, body, client):
        await _handle_approve(ack, body, client, ACTION_APPROVE_SEND)

    @bolt_app.action(ACTION_APPROVE_PUBLISH)
    async def handle_approve_publish(ack, body, client):
        await _handle_approve(ack, body, client, ACTION_APPROVE_PUBLISH)

    @bolt_app.action(ACTION_APPROVE_BOOK)
    async def handle_approve_book(ack, body, client):
        await _handle_approve(ack, body, client, ACTION_APPROVE_BOOK)

    @bolt_app.action(ACTION_DISCARD)
    async def handle_discard(ack, body, client):
        await _handle_discard(ack, body, client)

    @bolt_app.action(ACTION_REQUEST_EDIT)
    async def handle_request_edit(ack, body, client):
        await _handle_request_edit(ack, body, client)
