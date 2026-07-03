"""Discord adapter for approval card rendering.

Translates a backend-neutral ``ApprovalCard`` into a Discord message payload
(an embed + action-row buttons via discord.py's ``discord.ui.View`` pattern).

All Discord / discord.py specifics are confined to this module.
"""

from __future__ import annotations

from typing import Any

from router.approvals.card import ApprovalCard


class DiscordApprovalAdapter:
    """Renders ApprovalCard objects into Discord embed + button payloads.

    The returned dict has a ``"content"`` key (plain-text fallback) and an
    ``"embed"`` key (rich embed dict) suitable for passing to ``channel.send``
    via keyword unpacking.  A ``"view_spec"`` key carries the ordered list of
    button specs so the caller can construct a ``discord.ui.View`` without
    re-importing this module.
    """

    def render_approval_card(self, card: ApprovalCard) -> dict[str, Any]:
        """Render an ApprovalCard into a Discord message payload dict.

        Returns
        -------
        dict with keys:
          ``"content"``   — plain-text fallback (for clients that can't render embeds)
          ``"embed"``     — dict representation of a ``discord.Embed``
          ``"view_spec"`` — list[dict] of button specs:
                            ``{"label": str, "action_id": str, "style": str}``
        """
        agent_display = card.agent_name.capitalize()
        title = f"{agent_display} wants to {card.action_verb}"
        description = card.summary or self._format_payload(card.payload)

        embed: dict[str, Any] = {
            "title": title,
            "description": description,
            "color": 0x5865F2,  # Discord Blurple
            "fields": [
                {
                    "name": "Capability",
                    "value": f"{card.capability_instance} / {card.capability_type}",
                    "inline": True,
                },
                {
                    "name": "Agent",
                    "value": agent_display,
                    "inline": True,
                },
            ],
            "footer": {"text": f"draft_id: {card.draft_id}"},
        }

        if card.expires_at is not None:
            embed["fields"].append(
                {
                    "name": "Expires",
                    "value": card.expires_at.isoformat(),
                    "inline": True,
                }
            )

        view_spec = [
            {
                "label": str(getattr(action, "text", action)),
                "action_id": str(getattr(action, "action_id", "")),
                "style": str(getattr(action, "style", "secondary")),
            }
            for action in card.actions
        ]

        # Include draft_id in plain-text content so text-based approval is
        # possible on any transport: `aidt approve <draft_id>`.
        content = f"**{title}**\n{description}\ndraft_id: {card.draft_id}"

        return {
            "content": content,
            "embed": embed,
            "view_spec": view_spec,
        }

    @staticmethod
    def _format_payload(payload: dict[str, Any]) -> str:
        """Format the raw payload dict as a readable string for the embed description."""
        lines = []
        for key, value in payload.items():
            lines.append(f"**{key}**: {value}")
        return "\n".join(lines) if lines else "_No payload_"
