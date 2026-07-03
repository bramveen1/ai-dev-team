"""Transport-neutral handler for the ``approve`` and ``reject`` verbs.

Routes parsed approval commands to the existing
:func:`router.approvals.core.on_approval` path — the same path the Slack
card's Approve button already calls.  This is a second front door to the one
draft store; no new approval mechanism is introduced.

Disambiguation rules (from the issue-551 design note):

* Exactly **one** pending draft in the conversation thread →
  bare ``approve``/``reject`` (no id arg) acts on it.
* **More than one** pending draft in the thread →
  bare ``approve``/``reject`` is ambiguous → hard error listing the ids.
  Never guess.
* **Explicit** ``approve <draft-id>`` → always unambiguous, validated
  against the store.
* **Unknown id** → hard error, never silently no-op.

Auth stays in the handler layer (human-only gate in core.py, #658 pattern);
the parser does no auth.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from router.approvals.store import DraftStore
    from router.commands.types import Command, CommandResult

logger = logging.getLogger(__name__)


async def execute_approval_command(
    cmd: "Command",
    draft_store: "DraftStore | None",
) -> "CommandResult":
    """Route an ``approve`` or ``reject`` :class:`~router.commands.types.Command`.

    Parameters
    ----------
    cmd:
        Parsed command with ``verb`` in ``{"approve", "reject"}`` and
        ``conversation_ref`` identifying the thread.
    draft_store:
        The :class:`~router.approvals.store.DraftStore` instance.  When
        ``None``, returns an error (no store = cannot resolve drafts).
    """
    from router.approvals.core import on_approval
    from router.commands.types import CommandResult

    decision = "approved" if cmd.verb == "approve" else "discarded"

    if draft_store is None:
        return CommandResult(text="error: approval store unavailable", ok=False)

    explicit_id = cmd.args[0] if cmd.args else None

    if explicit_id:
        # Explicit draft-id: validate and act, regardless of conversation context.
        draft = draft_store.get(explicit_id)
        if draft is None or draft.status != "pending":
            return CommandResult(
                text=f"error: no pending draft {explicit_id!r} in this thread",
                ok=False,
            )
        updated = await on_approval(explicit_id, decision, draft_store)
        if updated is None:
            return CommandResult(
                text=f"error: draft {explicit_id!r} could not be {decision} (already resolved?)",
                ok=False,
            )
        return CommandResult(text=f"draft {explicit_id} {decision}")

    # No explicit id — resolve from conversation context.
    pending = draft_store.list_pending_for_conversation(cmd.conversation_ref)

    if not pending:
        return CommandResult(
            text="error: no pending drafts in this thread",
            ok=False,
        )

    if len(pending) > 1:
        ids = ", ".join(d.draft_id for d in pending)
        return CommandResult(
            text=(f"error: {len(pending)} pending drafts in this thread — reply 'approve <id>' (ids: {ids})"),
            ok=False,
        )

    # Exactly one pending draft.
    target = pending[0]
    updated = await on_approval(target.draft_id, decision, draft_store)
    if updated is None:
        return CommandResult(
            text=f"error: draft {target.draft_id!r} could not be {decision} (already resolved?)",
            ok=False,
        )
    return CommandResult(text=f"draft {target.draft_id} {decision}")
