"""Slack adapter for approval card rendering.

Wraps the existing block_kit.py functions so they are consumed through the
backend-neutral ApprovalAdapter interface.  All Slack / Block Kit specifics
are confined to this module and block_kit.py.

The render_approval_card output is byte-identical to calling
``build_approval_message_from_specs(draft, card.actions)`` directly with the
same draft data — verified by the parity test in tests/unit/approvals/test_adapters.py.
"""

from __future__ import annotations

from typing import Any

from router.approvals.block_kit import build_approval_message_from_specs
from router.approvals.card import ApprovalCard
from router.approvals.store import Draft


class SlackApprovalAdapter:
    """Renders ApprovalCard objects into Slack Block Kit message payloads.

    Wraps the existing block_kit.py rendering functions so that call sites
    can use the backend-neutral ApprovalCard DTO without knowing about Block Kit.
    """

    def render_approval_card(self, card: ApprovalCard) -> dict[str, Any]:
        """Render an ApprovalCard into a Slack Block Kit message payload.

        Produces output byte-identical to
        ``build_approval_message_from_specs(draft, card.actions)``
        given the same underlying data.

        Returns a dict with a ``"blocks"`` key, ready for ``chat.postMessage``.
        """
        draft = Draft(
            draft_id=card.draft_id,
            agent_name=card.agent_name,
            capability_type=card.capability_type,
            capability_instance=card.capability_instance,
            action_verb=card.action_verb,
            payload=card.payload,
            slack_channel="",
            slack_message_ts="",
        )
        return build_approval_message_from_specs(draft, card.actions)
