"""Discord interactivity handlers for approval flow actions (#682).

Mirrors ``router/approvals/handlers.py`` (Slack) for the Discord transport.
Approval cards are posted via a raw REST call from ``internal_api.py``, not
through a live ``discord.ui.View`` send, so button clicks are not routed by
discord.py's normal View dispatch. Instead each ``DiscordAdapter`` registers
``_on_interaction`` as an extra ``on_interaction`` listener (see
``DiscordAdapter.register_interaction_handler``); it recognizes approval
button clicks by their ``custom_id`` prefix and delegates to the same
backend-agnostic ``on_approval()`` core Slack uses.

Each handler:
1. Responds to the interaction (edit_message / send_message) within
   Discord's 3-second ack window.
2. Delegates core approval logic to ``on_approval()`` (backend-agnostic).
3. Updates the Discord message with the outcome via the approvals adapter.
4. For direct/pack-backed approvals: invokes the execute callback.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, Awaitable, Callable

with warnings.catch_warnings():
    # See router/chat/adapters/discord.py — discord.py transitively imports
    # the stdlib ``audioop`` module, which warns on Python 3.11.
    warnings.simplefilter("ignore", DeprecationWarning)
    import discord

from router.approvals.adapters.discord import CUSTOM_ID_PREFIX, DiscordApprovalAdapter
from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_REQUEST_EDIT,
)
from router.approvals.core import DraftCleanupCallback, on_approval
from router.approvals.store import Draft, DraftStore

logger = logging.getLogger(__name__)

_APPROVE_ACTION_IDS = frozenset({ACTION_APPROVE_SEND, ACTION_APPROVE_PUBLISH, ACTION_APPROVE_BOOK})

# Type alias for the post-approval execute callback. Receives the approved
# Draft and the triggering discord.Interaction. Parity with handlers.py's
# DraftExecuteCallback, but Discord has no Slack-shaped (channel, thread_ts,
# client) triple — implementations resolve their own posting context from
# the draft/interaction instead.
DraftExecuteCallback = Callable[[Draft, Any], Awaitable[None]]

# Module-level callback references, set by register_handlers().
_store: DraftStore | None = None
_cleanup_callback: DraftCleanupCallback | None = None
_execute_callback: DraftExecuteCallback | None = None


def _get_store() -> DraftStore:
    """Return the module-level store, raising if not initialized."""
    if _store is None:
        raise RuntimeError("Discord approval handlers not registered — call register_handlers() first")
    return _store


def _parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Split ``"approval:<action_id>:<draft_id>"`` into ``(action_id, draft_id)``.

    Returns ``None`` for anything that isn't a recognized approval button
    (e.g. a ``prompt_for_choice`` button, or a click on stale/foreign UI).
    """
    parts = custom_id.split(":", 2)
    if len(parts) != 3 or parts[0] != CUSTOM_ID_PREFIX:  # noqa: PLR2004
        return None
    _, action_id, draft_id = parts
    if not action_id or not draft_id:
        return None
    return action_id, draft_id


async def _post_already_resolved_notice(draft_id: str, interaction: Any) -> None:
    """Ephemeral reply when a draft was already resolved or expired.

    Covers both the pre-click early-return path and the TOCTOU race.
    Silent no-op when the draft is not found (invalid ID).
    """
    existing = _get_store().get(draft_id)
    if existing is None or existing.status == "pending":
        return
    try:
        await interaction.response.send_message(
            f"This draft has already been {existing.status} and cannot be actioned again.",
            ephemeral=True,
        )
    except Exception:
        logger.exception("Failed to post already-resolved notice for draft %s", draft_id)


async def _handle_approve(interaction: Any, action_id: str, draft_id: str) -> None:
    """Handle an approve-family button click (send/publish/book)."""
    logger.info("Discord approval action=%s draft_id=%s", action_id, draft_id)

    draft = await on_approval(draft_id, "approved", _get_store())
    if draft is None:
        await _post_already_resolved_notice(draft_id, interaction)
        return

    outcome_embed = DiscordApprovalAdapter.render_outcome_embed(draft, approved=True)
    try:
        await interaction.response.edit_message(embed=discord.Embed.from_dict(outcome_embed), view=None)
    except Exception:
        logger.exception("Failed to update Discord message for draft %s", draft_id)

    logger.info("Draft %s approved and message updated", draft_id)

    # Execution: for direct (pack-backed) drafts the agent still needs to
    # actually run the action — same contract as the Slack handler.
    if draft.draft_type == "direct" and _execute_callback is not None:
        try:
            await _execute_callback(draft, interaction)
        except Exception:
            logger.exception("Execute callback failed for draft %s", draft_id)


async def _handle_discard(interaction: Any, draft_id: str) -> None:
    """Handle the discard button click."""
    logger.info("Discord discard action draft_id=%s", draft_id)

    draft = await on_approval(draft_id, "discarded", _get_store(), cleanup_callback=_cleanup_callback)
    if draft is None:
        await _post_already_resolved_notice(draft_id, interaction)
        return

    outcome_embed = DiscordApprovalAdapter.render_outcome_embed(draft, approved=False)
    try:
        await interaction.response.edit_message(embed=discord.Embed.from_dict(outcome_embed), view=None)
    except Exception:
        logger.exception("Failed to update Discord message for draft %s", draft_id)

    logger.info("Draft %s discarded and message updated", draft_id)


async def _handle_request_edit(interaction: Any, draft_id: str) -> None:
    """Handle the edit request — reply asking for clarification (draft stays pending)."""
    store = _get_store()
    logger.info("Discord edit request for draft_id=%s", draft_id)

    draft = store.get(draft_id)
    if draft is None:
        logger.warning("Draft %s not found for edit request", draft_id)
        return

    if draft.status != "pending":
        logger.info("Draft %s already resolved (status=%s), skipping", draft_id, draft.status)
        await _post_already_resolved_notice(draft_id, interaction)
        return

    try:
        await interaction.response.send_message(
            f"What changes would you like to make to this {draft.capability_type} draft?"
        )
    except Exception:
        logger.exception("Failed to post edit request for draft %s", draft_id)


async def _on_interaction(interaction: Any) -> None:
    """Route a Discord component interaction to the matching approval handler.

    Fires for every ``INTERACTION_CREATE`` event on the adapter's client;
    non-component interactions and non-approval custom_ids are ignored so
    this composes with other interaction sources (e.g. ``prompt_for_choice``).
    """
    if getattr(interaction, "type", None) is not discord.InteractionType.component:
        return

    data = getattr(interaction, "data", None) or {}
    custom_id = data.get("custom_id", "")
    parsed = _parse_custom_id(custom_id)
    if parsed is None:
        return

    action_id, draft_id = parsed
    if _store is None:
        logger.warning("Discord approval interaction received before register_handlers(); dropping draft=%s", draft_id)
        return

    if action_id in _APPROVE_ACTION_IDS:
        await _handle_approve(interaction, action_id, draft_id)
    elif action_id == ACTION_DISCARD:
        await _handle_discard(interaction, draft_id)
    elif action_id == ACTION_REQUEST_EDIT:
        await _handle_request_edit(interaction, draft_id)
    else:
        logger.warning("Unknown approval action_id=%r draft=%s", action_id, draft_id)


def register_handlers(
    adapter: Any,
    store: DraftStore,
    cleanup_callback: DraftCleanupCallback | None = None,
    execute_callback: DraftExecuteCallback | None = None,
) -> None:
    """Register the approval interaction handler on a Discord adapter.

    Args:
        adapter: The DiscordAdapter instance (one per agent).
        store: The DraftStore instance for persisting draft state — shared
            module-level state, same pattern as ``approvals.handlers``, since
            every per-agent adapter draws from the one router-wide store.
        cleanup_callback: Optional async callback to delete external draft
            resources when a user clicks Discard.
        execute_callback: Optional async callback invoked after a successful
            approval on a *direct* draft. Receives ``(draft, interaction)``.
    """
    global _store, _cleanup_callback, _execute_callback
    _store = store
    _cleanup_callback = cleanup_callback
    _execute_callback = execute_callback

    adapter.register_interaction_handler(_on_interaction)
