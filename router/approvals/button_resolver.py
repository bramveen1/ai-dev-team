"""Pack-aware button resolver for the approval flow.

Two paths produce a draft, and each renders different buttons:

- **Pack-backed.** The agent holds a pack (e.g. ``github``); the pack's
  ``approve:`` list names the verbs that need a human to authorize. When the
  agent emits a draft for one of those verbs, we render a primary action
  button — clicking it both approves the draft and triggers execution via
  the pack's MCP server / CLI. ``approve:`` is the source of truth; the old
  per-instance ``permissions:`` map is gone.

- **Connector-backed.** Native Claude connectors (Microsoft 365, Gmail,
  Notion, …) have no pack — the agent already has the connector tools.
  When something needs approval there, the connector itself can't be
  driven from Slack, so we render an "Open in <App>" deep-link button and
  let the user finish in the native app.

The draft block carries either ``pack`` (pack-backed) or ``target``
(connector-backed). For one cycle of backwards compatibility, drafts may
still arrive with the legacy ``capability_type`` / ``capability_instance``
fields (handled in :mod:`router.approvals.interceptor`); those land here
as ``pack=None, target=None`` and we render a discard-only fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.approvals.block_kit import (
    ACTION_APPROVE_BOOK,
    ACTION_APPROVE_PUBLISH,
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_OPEN_IN_APP,
    ACTION_REQUEST_EDIT,
)
from router.packs.loader import Pack


@dataclass
class ButtonSpec:
    """Specification for a single approval button."""

    action_id: str
    text: str
    style: str = "default"  # "primary", "danger", or "default"
    url: str | None = None


# Maps action verb to the approval action_id on the resulting button.
_VERB_ACTION_MAP: dict[str, str] = {
    "send": ACTION_APPROVE_SEND,
    "publish": ACTION_APPROVE_PUBLISH,
    "book": ACTION_APPROVE_BOOK,
    # Pack-backed verbs that don't have a dedicated action_id reuse
    # ACTION_APPROVE_SEND — the existing handler just transitions the
    # draft to ``approved`` and the agent picks it up to execute.
    "merge": ACTION_APPROVE_SEND,
}

# Connector-backed targets → human-readable app name for "Open in <App>".
# Keep this list short and concrete: every entry is a connector that
# Claude exposes natively and where there's a sensible deep-link.
_APP_NAMES: dict[str, str] = {
    "outlook": "Outlook",
    "outlook-calendar": "Outlook Calendar",
    "gmail": "Gmail",
    "google-calendar": "Google Calendar",
    "google-drive": "Google Drive",
    "zoho": "Zoho Mail",
    "buffer": "Buffer",
    "twitter": "Twitter",
    "linkedin": "LinkedIn",
    "figma": "Figma",
    "notion": "Notion",
    "linear": "Linear",
}


def get_app_name(target: str) -> str:
    """Return the human-readable name for a connector target."""
    return _APP_NAMES.get(target, target)


def _direct_action_buttons(action_verb: str) -> list[ButtonSpec]:
    approve_action = _VERB_ACTION_MAP.get(action_verb, ACTION_APPROVE_SEND)
    return [
        ButtonSpec(
            action_id=approve_action,
            text=action_verb.capitalize(),
            style="primary",
        ),
        ButtonSpec(action_id=ACTION_REQUEST_EDIT, text="Edit"),
        ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
    ]


def _deep_link_buttons(target: str, deep_link_url: str | None) -> list[ButtonSpec]:
    return [
        ButtonSpec(
            action_id=ACTION_OPEN_IN_APP,
            text=f"Open in {get_app_name(target)}",
            style="primary",
            url=deep_link_url,
        ),
        ButtonSpec(action_id=ACTION_REQUEST_EDIT, text="Redraft"),
        ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger"),
    ]


def _discard_only() -> list[ButtonSpec]:
    return [ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger")]


def resolve_buttons(
    *,
    action_verb: str,
    pack: Pack | None = None,
    target: str | None = None,
    deep_link_url: str | None = None,
) -> list[ButtonSpec]:
    """Compute the button set for a draft approval message.

    - ``pack`` is set → pack-backed draft. If the verb is in
      ``pack.approve``, render a direct action button (clicking
      approves *and* triggers the agent to execute). If the verb isn't
      in ``approve``, the agent shouldn't have drafted in the first
      place — fall back to discard-only as a safety net.
    - ``target`` is set → connector-backed draft. Render an
      ``Open in <App>`` deep-link button so the user can finish the
      action in the native app.
    - Neither → legacy / unrecognized shape. Render discard-only.

    ``pack`` and ``target`` are mutually exclusive in normal flow; if
    both are set, ``pack`` wins (pack-backed is the more specific path).
    """
    if pack is not None:
        if action_verb in pack.approve:
            return _direct_action_buttons(action_verb)
        return _discard_only()
    if target is not None:
        return _deep_link_buttons(target, deep_link_url)
    return _discard_only()
