"""Approval adapter protocol and registry.

An adapter translates a backend-neutral ``ApprovalCard`` into a
backend-specific message payload.  New backends (Terminal, Discord) implement
``ApprovalAdapter`` and are registered here.
"""

from __future__ import annotations

from typing import Any, Protocol

from router.approvals.card import ApprovalCard


class ApprovalAdapter(Protocol):
    """Protocol for backend-specific approval card renderers."""

    def render_approval_card(self, card: ApprovalCard) -> dict[str, Any]:
        """Render an ApprovalCard into a backend-specific message payload.

        Parameters
        ----------
        card : the backend-neutral approval card

        Returns
        -------
        Backend-specific message dict (e.g. ``{"blocks": [...]}`` for Slack).
        """
        ...
