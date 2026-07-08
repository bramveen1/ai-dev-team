"""Discord adapter for approval card rendering.

Translates a backend-neutral ``ApprovalCard`` into a Discord message payload
(an embed + action-row buttons via discord.py's ``discord.ui.View`` pattern).

All Discord / discord.py specifics are confined to this module.
"""

from __future__ import annotations

from typing import Any

from router.approvals.card import ApprovalCard
from router.approvals.store import Draft

# Prefix for button custom_ids so the interaction handler can recognize an
# approval click regardless of which code path posted the card (#682).
CUSTOM_ID_PREFIX = "approval"

# Discord button "style" ints (discord.ButtonStyle): Primary=1, Secondary=2,
# Success=3, Danger=4, Link=5. Slack's "primary" renders green, so it maps to
# Success rather than Discord's blurple Primary for the closest visual parity.
_BUTTON_STYLE_BY_NAME = {"primary": 3, "danger": 4, "default": 2}
_BUTTON_STYLE_LINK = 5
_BUTTON_STYLE_SECONDARY = 2


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
            "components": self.render_components(card),
        }

    def render_components(self, card: ApprovalCard) -> list[dict[str, Any]]:
        """Build the Discord API ``components`` payload (one action row) for *card*.

        Buttons are stamped with a ``custom_id`` encoding the action + draft id
        (``"approval:<action_id>:<draft_id>"``) so a click round-trips through
        the interaction handler even though the card is posted via a raw REST
        call rather than a live ``discord.ui.View`` send (#682). Actions that
        carry a ``url`` (connector deep-links) render as Link-style buttons
        instead — Discord forbids a ``custom_id`` on those.
        """
        buttons: list[dict[str, Any]] = []
        for action in card.actions:
            action_id = str(getattr(action, "action_id", ""))
            if not action_id:
                continue
            label = str(getattr(action, "text", action_id))
            url = getattr(action, "url", None)
            button: dict[str, Any] = {"type": 2, "label": label}
            if url:
                button["style"] = _BUTTON_STYLE_LINK
                button["url"] = url
            else:
                style_key = str(getattr(action, "style", "default"))
                button["style"] = _BUTTON_STYLE_BY_NAME.get(style_key, _BUTTON_STYLE_SECONDARY)
                button["custom_id"] = f"{CUSTOM_ID_PREFIX}:{action_id}:{card.draft_id}"
            buttons.append(button)

        if not buttons:
            return []
        return [{"type": 1, "components": buttons}]

    @staticmethod
    def render_outcome_embed(draft: Draft, approved: bool) -> dict[str, Any]:
        """Build the Discord embed shown after a draft is resolved.

        Mirrors ``block_kit.build_outcome_message`` — used to replace the
        original approval card (buttons cleared) once a decision lands.
        """
        agent_display = draft.agent_name.capitalize()
        if approved:
            resolved_time = draft.resolved_at.strftime("%I:%M %p") if draft.resolved_at else "just now"
            description = f":white_check_mark: {draft.action_verb.capitalize()}ed at {resolved_time}"
            color = 0x2ECC71  # green
        else:
            description = ":x: Discarded"
            color = 0xE74C3C  # red

        return {
            "title": f"{agent_display} — {draft.action_verb}",
            "description": description,
            "color": color,
            "footer": {"text": f"draft_id: {draft.draft_id}"},
        }

    @staticmethod
    def _format_payload(payload: dict[str, Any]) -> str:
        """Format the raw payload dict as a readable string for the embed description."""
        lines = []
        for key, value in payload.items():
            lines.append(f"**{key}**: {value}")
        return "\n".join(lines) if lines else "_No payload_"
