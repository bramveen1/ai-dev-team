"""Backend-neutral ApprovalCard DTO for the approval flow.

ApprovalCard is a render-facing projection of (Draft + resolved ButtonSpecs).
It carries all the information a backend adapter needs to render an approval
message, without coupling to any specific transport or UI framework.

Does NOT import from block_kit or slack_sdk — those are Slack adapter concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ApprovalCard:
    """Backend-neutral projection of (Draft + resolved ButtonSpecs).

    Constructed by the call site that creates a draft approval message.
    Adapters consume this DTO to produce backend-specific payloads.

    Fields
    ------
    draft_id         : persistent draft identifier
    agent_name       : the agent that created the draft (e.g. "lisa")
    pack             : pack name for pack-backed drafts; None for connector-backed
    capability_type  : e.g. "email", "calendar", "pack"
    capability_instance : e.g. "mine", "outlook", "dispatch"
    action_verb      : e.g. "send", "merge", "dispatch_issue"
    summary          : pre-formatted human-readable payload preview (for plain-text backends)
    payload          : raw draft payload dict
    actions          : list[ButtonSpec] resolved by button_resolver
    expires_at       : optional expiry timestamp
    requires_followup: reserved for Discord's 15-min interaction-token TTL — do not use yet
    """

    draft_id: str
    agent_name: str
    pack: str | None
    capability_type: str
    capability_instance: str
    action_verb: str
    summary: str
    payload: dict[str, Any]
    actions: list = field(default_factory=list)  # list[ButtonSpec]
    expires_at: datetime | None = None
    requires_followup: bool = False  # reserved for Discord's 15-min interaction-token TTL
