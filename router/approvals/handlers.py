"""Slack interactivity handlers for approval flow actions.

Each handler is a thin Slack-Bolt shim that:
1. ``ack()`` immediately (meet Slack's 3-second requirement)
2. Delegates core approval logic to ``on_approval()`` (backend-agnostic)
3. Updates the Slack message with the outcome via block_kit.py
4. For direct/pack-backed approvals: invokes the execute callback with Slack context

Handlers are registered with a slack_bolt AsyncApp via register_handlers().
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_REQUEST_EDIT,
    build_outcome_message,
)
from router.approvals.core import DraftCleanupCallback, on_approval
from router.approvals.store import Draft, DraftStore
from router.chat.adapters import slack_outbound

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

# Type alias for the post-approval execute callback.
# Receives the approved Draft + Slack thread context. Implementations
# typically re-dispatch to the owning agent with a synthesized message
# instructing it to run the previously-drafted action (e.g. ``gh pr merge``).
# The router never executes pack actions itself — the agent does, in its
# container, with its env, so the same auth/scope rules apply as during
# drafting.
DraftExecuteCallback = Callable[[Draft, str, str, Any], Awaitable[None]]

# Module-level callback references, set by register_handlers()
_store: DraftStore | None = None
_cleanup_callback: DraftCleanupCallback | None = None
_execute_callback: DraftExecuteCallback | None = None


def _get_store() -> DraftStore:
    """Return the module-level store, raising if not initialized."""
    if _store is None:
        raise RuntimeError("Approval handlers not registered — call register_handlers() first")
    return _store


async def _post_already_resolved_notice(draft_id: str, body: dict, client: Any) -> None:
    """Post a thread notice when a draft was already resolved or expired.

    Only fires when the draft exists in the store but is no longer pending,
    covering both the pre-click early-return path and the TOCTOU race.
    Silent no-op when the draft is not found (invalid ID).
    """
    existing = _get_store().get(draft_id)
    if existing is None or existing.status == "pending":
        return

    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    text = f"This draft has already been {existing.status} and cannot be actioned again."
    try:
        if slack_outbound.enabled():
            await slack_outbound.send(client, existing.agent_name, text, channel=channel, thread_ts=message_ts)
        else:
            await client.chat_postMessage(channel=channel, thread_ts=message_ts, text=text)
    except Exception:
        logger.exception("Failed to post already-resolved notice for draft %s", draft_id)


async def _handle_approve(ack: Any, body: dict, client: Any, action_id: str) -> None:
    """Thin Slack-Bolt shim for all approve actions (send, publish, book)."""
    await ack()

    draft_id = body["actions"][0]["value"]
    logger.info("Approval action=%s draft_id=%s", action_id, draft_id)

    draft = await on_approval(draft_id, "approved", _get_store())
    if draft is None:
        await _post_already_resolved_notice(draft_id, body, client)
        return

    # Slack-specific: edit the message to show the outcome.
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

    # Execution: for direct (pack-backed) drafts the agent still needs to
    # actually run the action. Re-dispatch via the registered callback so
    # the agent's container does the work — same env, same auth, same prompt.
    # Native (connector-backed) drafts already exist in the external app
    # and the user is expected to finish them there, so no execution.
    if draft.draft_type == "direct" and _execute_callback is not None:
        thread_ts = body.get("message", {}).get("thread_ts") or message_ts
        try:
            await _execute_callback(draft, channel, thread_ts, client)
        except Exception:
            logger.exception("Execute callback failed for draft %s", draft_id)


async def _handle_discard(ack: Any, body: dict, client: Any) -> None:
    """Thin Slack-Bolt shim for the discard action.

    Delegates cleanup + transition to on_approval(), then updates the message.
    """
    await ack()

    draft_id = body["actions"][0]["value"]
    logger.info("Discard action draft_id=%s", draft_id)

    draft = await on_approval(draft_id, "discarded", _get_store(), cleanup_callback=_cleanup_callback)
    if draft is None:
        await _post_already_resolved_notice(draft_id, body, client)
        return

    # Slack-specific: edit the message to show the outcome.
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
        await _post_already_resolved_notice(draft_id, body, client)
        return

    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    text = f"What changes would you like to make to this {draft.capability_type} draft?"

    try:
        if slack_outbound.enabled():
            await slack_outbound.send(client, draft.agent_name, text, channel=channel, thread_ts=message_ts)
        else:
            await client.chat_postMessage(channel=channel, thread_ts=message_ts, text=text)
    except Exception:
        logger.exception("Failed to post edit request thread for draft %s", draft_id)


def register_handlers(
    bolt_app: AsyncApp,
    store: DraftStore,
    cleanup_callback: DraftCleanupCallback | None = None,
    execute_callback: DraftExecuteCallback | None = None,
) -> None:
    """Register all approval action handlers with the Slack bolt app.

    Args:
        bolt_app: The slack_bolt AsyncApp instance.
        store: The DraftStore instance for persisting draft state.
        cleanup_callback: Optional async callback to delete external draft resources
            (e.g. M365 drafts) when a user clicks Discard. Receives the Draft object.
        execute_callback: Optional async callback invoked after a successful
            approval on a *direct* draft. Receives ``(draft, channel, thread_ts,
            client)`` and is expected to drive the agent that produced the draft
            to actually execute the action (e.g. run ``gh pr merge``).
    """
    global _store, _cleanup_callback, _execute_callback
    _store = store
    _cleanup_callback = cleanup_callback
    _execute_callback = execute_callback

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
