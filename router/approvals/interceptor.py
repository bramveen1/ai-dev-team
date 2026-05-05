"""Response interceptor for the approval flow.

Parses agent responses for ``draft-approval`` code-fence blocks,
creates Draft records, resolves pack-aware buttons, and posts Block
Kit approval messages to Slack.

Agents include a structured ``draft-approval`` JSON block in their
response when they create a draft via an MCP tool. The interceptor
strips that block from the visible response and routes it through the
approval flow.

Schema (PR 6 onward):

```json
{
  "draft_id": "<id>",
  "action_verb": "<send|publish|book|merge>",
  "payload": {...},
  // exactly one of these — pack-backed vs connector-backed:
  "pack":   "<pack-name>",   // e.g. "github", "zoho-mail"
  "target": "<connector>",   // e.g. "outlook", "gmail"
}
```

Legacy fields ``capability_type`` / ``capability_instance`` are still
accepted for one cycle of backwards compatibility — they pass through
to the resolver as ``pack=None, target=None``, which falls back to a
discard-only button set. Drop in PR 8 (issue #88).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from router.approvals.block_kit import build_approval_message_from_specs
from router.approvals.button_resolver import resolve_buttons
from router.approvals.deep_links import get_deep_link
from router.approvals.expiration_worker import get_ttl
from router.approvals.store import Draft, DraftStore
from router.packs.loader import Pack, discover_packs

logger = logging.getLogger(__name__)

# Regex to match ```draft-approval ... ``` fenced blocks.
_DRAFT_BLOCK_RE = re.compile(
    r"```draft-approval\s*\n(.*?)\n```",
    re.DOTALL,
)

# Always-required fields in any draft-approval block, old schema or new.
_ALWAYS_REQUIRED = {"draft_id", "action_verb", "payload"}


@dataclass
class DraftRequest:
    """A parsed draft approval request extracted from agent response text.

    Either ``pack`` or ``target`` should be set for new-schema drafts.
    The legacy ``capability_type`` / ``capability_instance`` fields are
    retained for one release cycle so the DB schema and any in-flight
    drafts keep working.
    """

    draft_id: str
    action_verb: str
    payload: dict[str, Any]
    pack: str | None = None
    target: str | None = None
    capability_type: str = ""
    capability_instance: str = ""


@dataclass
class InterceptResult:
    """Result of parsing an agent response for draft-approval blocks."""

    cleaned_text: str
    draft_requests: list[DraftRequest] = field(default_factory=list)

    @property
    def has_drafts(self) -> bool:
        return len(self.draft_requests) > 0


def parse_response(response_text: str) -> InterceptResult:
    """Parse an agent response for draft-approval code-fence blocks.

    Extracts all ``draft-approval`` blocks, validates the JSON, and
    returns an InterceptResult with the cleaned text and parsed
    requests. Malformed blocks are logged and silently stripped.
    """
    draft_requests: list[DraftRequest] = []

    def _replace_block(match: re.Match) -> str:
        raw_json = match.group(1).strip()
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("Malformed JSON in draft-approval block: %s", raw_json[:200])
            return ""

        missing = _ALWAYS_REQUIRED - set(data.keys())
        if missing:
            logger.warning("draft-approval block missing fields %s: %s", missing, raw_json[:200])
            return ""

        # New schema requires ``pack`` xor ``target``. Legacy is allowed
        # via capability_type + capability_instance; the resolver later
        # falls back to discard-only for that case.
        has_pack = bool(data.get("pack"))
        has_target = bool(data.get("target"))
        legacy_cap_type = data.get("capability_type")
        legacy_cap_inst = data.get("capability_instance")
        has_legacy = bool(legacy_cap_type) and bool(legacy_cap_inst)

        if not (has_pack or has_target or has_legacy):
            logger.warning(
                "draft-approval block needs one of {pack, target, capability_type+capability_instance}: %s",
                raw_json[:200],
            )
            return ""

        payload = data["payload"] if isinstance(data["payload"], dict) else {}

        draft_requests.append(
            DraftRequest(
                draft_id=str(data["draft_id"]),
                action_verb=str(data["action_verb"]),
                payload=payload,
                pack=str(data["pack"]) if has_pack else None,
                target=str(data["target"]) if has_target else None,
                capability_type=str(legacy_cap_type) if legacy_cap_type else "",
                capability_instance=str(legacy_cap_inst) if legacy_cap_inst else "",
            )
        )
        return ""

    cleaned = _DRAFT_BLOCK_RE.sub(_replace_block, response_text).strip()
    return InterceptResult(cleaned_text=cleaned, draft_requests=draft_requests)


def _resolve_pack(pack_name: str | None, packs_dir: Path | None) -> Pack | None:
    """Look up a Pack by name. Returns ``None`` if the name is unknown."""
    if not pack_name:
        return None
    packs = discover_packs(packs_dir)
    return packs.get(pack_name)


def _draft_display_fields(draft_request: DraftRequest, pack: Pack | None) -> tuple[str, str]:
    """Return (capability_type, capability_instance) for the Draft record.

    These fields persist to SQLite and feed the approval-message header
    ("via *X* (Y)") plus the expiration worker's TTL bucket. We map the
    new schema into them so downstream code keeps working unchanged:

    - pack-backed → capability_type=``pack``, capability_instance=<pack name>
    - connector-backed → capability_type=``connector``, capability_instance=<target>
    - legacy → keep whatever the agent sent
    """
    if pack is not None:
        return ("pack", pack.name)
    if draft_request.target:
        return ("connector", draft_request.target)
    return (draft_request.capability_type, draft_request.capability_instance)


async def post_approval_message(
    draft_request: DraftRequest,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client: Any,
    store: DraftStore,
    *,
    packs_dir: Path | None = None,
    ttl_config: dict[str, Any] | None = None,
) -> Draft:
    """Post a Block Kit approval message and persist the Draft record.

    Pack lookup, deep-link generation, and button rendering all happen
    here so callers (the dispatcher / Slack handler) don't need to know
    anything about packs.
    """
    pack = _resolve_pack(draft_request.pack, packs_dir)

    # Determine draft type:
    # - pack-backed + verb in approve list: agent will execute on click → "direct"
    # - connector-backed (or anything else): user takes over in native app → "native"
    is_direct = pack is not None and draft_request.action_verb in pack.approve
    draft_type = "direct" if is_direct else "native"

    # Deep-link URL only matters for the connector path.
    deep_link_url = None
    if pack is None and draft_request.target:
        deep_link_url = get_deep_link(draft_request.target, draft_request.draft_id)

    button_specs = resolve_buttons(
        action_verb=draft_request.action_verb,
        pack=pack,
        target=draft_request.target,
        deep_link_url=deep_link_url,
    )

    cap_type, cap_instance = _draft_display_fields(draft_request, pack)

    ttl = get_ttl(cap_type, ttl_config)
    now = datetime.now(timezone.utc)
    expires_at = now + ttl

    temp_draft_id = str(uuid.uuid4())
    temp_draft = Draft(
        draft_id=temp_draft_id,
        agent_name=agent_name,
        capability_type=cap_type,
        capability_instance=cap_instance,
        action_verb=draft_request.action_verb,
        payload=draft_request.payload,
        slack_channel=channel,
        slack_message_ts="",  # Filled after posting
        draft_type=draft_type,
        external_id=draft_request.draft_id if draft_type == "native" else None,
        created_at=now,
        expires_at=expires_at,
    )

    approval_msg = build_approval_message_from_specs(temp_draft, button_specs)

    result = await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=approval_msg["blocks"],
        text=f"{agent_name.capitalize()} wants to {draft_request.action_verb}",
    )
    message_ts = result["ts"]

    temp_draft.slack_message_ts = message_ts
    store.create(temp_draft)

    logger.info(
        "Posted approval message draft=%s agent=%s pack=%s target=%s verb=%s type=%s",
        temp_draft_id,
        agent_name,
        draft_request.pack,
        draft_request.target,
        draft_request.action_verb,
        draft_type,
    )

    return temp_draft
