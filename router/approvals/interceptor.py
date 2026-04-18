"""Response interceptor for the approval flow.

Parses agent responses for ``draft-approval`` code-fence blocks,
creates Draft records, resolves permission-aware buttons, and posts
Block Kit approval messages to Slack.

Agents include a structured ``draft-approval`` JSON block in their
response when they create a draft via an MCP tool. The interceptor
strips that block from the visible response and routes it through
the approval flow.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from router.approvals.block_kit import build_approval_message_from_specs
from router.approvals.button_resolver import resolve_buttons
from router.approvals.deep_links import get_deep_link
from router.approvals.expiration_worker import get_ttl
from router.approvals.store import Draft, DraftStore

logger = logging.getLogger(__name__)

# Regex to match ```draft-approval ... ``` fenced blocks.
# Captures the JSON content between the fences.
_DRAFT_BLOCK_RE = re.compile(
    r"```draft-approval\s*\n(.*?)\n```",
    re.DOTALL,
)

# Required fields in the draft-approval JSON.
_REQUIRED_FIELDS = {"draft_id", "capability_type", "capability_instance", "action_verb", "payload"}


@dataclass
class DraftRequest:
    """A parsed draft approval request extracted from agent response text."""

    draft_id: str
    capability_type: str
    capability_instance: str
    action_verb: str
    payload: dict[str, Any]


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
    returns an InterceptResult with the cleaned text and parsed requests.
    Malformed blocks are logged and silently stripped.
    """
    draft_requests: list[DraftRequest] = []

    def _replace_block(match: re.Match) -> str:
        raw_json = match.group(1).strip()
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("Malformed JSON in draft-approval block: %s", raw_json[:200])
            return ""

        missing = _REQUIRED_FIELDS - set(data.keys())
        if missing:
            logger.warning("draft-approval block missing fields %s: %s", missing, raw_json[:200])
            return ""

        draft_requests.append(
            DraftRequest(
                draft_id=str(data["draft_id"]),
                capability_type=data["capability_type"],
                capability_instance=data["capability_instance"],
                action_verb=data["action_verb"],
                payload=data["payload"] if isinstance(data["payload"], dict) else {},
            )
        )
        return ""

    cleaned = _DRAFT_BLOCK_RE.sub(_replace_block, response_text).strip()
    return InterceptResult(cleaned_text=cleaned, draft_requests=draft_requests)


async def post_approval_message(
    draft_request: DraftRequest,
    agent_name: str,
    channel: str,
    thread_ts: str,
    client: Any,
    store: DraftStore,
    capability_instance: Any | None = None,
    ttl_config: dict[str, Any] | None = None,
) -> Draft:
    """Post a Block Kit approval message and persist the Draft record.

    Args:
        draft_request: The parsed draft request from the agent response.
        agent_name: The agent that produced the draft.
        channel: Slack channel ID for posting.
        thread_ts: Slack thread timestamp for threading.
        client: Slack WebClient instance.
        store: DraftStore for persistence.
        capability_instance: Optional CapabilityInstance for permission-aware buttons.
            When None, buttons default to the "no permission" set (Open in App + Redraft + Discard).
        ttl_config: Optional TTL config dict for expiration calculation.

    Returns:
        The persisted Draft record.
    """
    # Determine draft type: "native" when agent lacks the action permission, "direct" otherwise
    has_permission = False
    if capability_instance is not None:
        has_permission = draft_request.action_verb in capability_instance.permissions

    draft_type = "direct" if has_permission else "native"

    # Generate deep link for native drafts
    deep_link_url = None
    if not has_permission and capability_instance is not None:
        deep_link_url = get_deep_link(
            draft_request.capability_type,
            capability_instance.provider,
            draft_request.draft_id,
        )

    # Resolve permission-aware buttons
    if capability_instance is not None:
        button_specs = resolve_buttons(
            capability_type=draft_request.capability_type,
            capability_instance=capability_instance,
            action_verb=draft_request.action_verb,
            deep_link_url=deep_link_url,
        )
    else:
        # Fallback: no capability info — show safe defaults (discard only)
        from router.approvals.button_resolver import ACTION_DISCARD, ButtonSpec

        button_specs = [ButtonSpec(action_id=ACTION_DISCARD, text="Discard", style="danger")]

    # Compute expiration time
    ttl = get_ttl(draft_request.capability_type, ttl_config)
    now = datetime.now(timezone.utc)
    expires_at = now + ttl

    # Build a temporary draft to render the approval message
    temp_draft_id = str(uuid.uuid4())
    temp_draft = Draft(
        draft_id=temp_draft_id,
        agent_name=agent_name,
        capability_type=draft_request.capability_type,
        capability_instance=draft_request.capability_instance,
        action_verb=draft_request.action_verb,
        payload=draft_request.payload,
        slack_channel=channel,
        slack_message_ts="",  # Filled after posting
        draft_type=draft_type,
        external_id=draft_request.draft_id if draft_type == "native" else None,
        created_at=now,
        expires_at=expires_at,
    )

    # Build the Block Kit message
    approval_msg = build_approval_message_from_specs(temp_draft, button_specs)

    # Post the approval message to Slack
    result = await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=approval_msg["blocks"],
        text=f"{agent_name.capitalize()} wants to {draft_request.action_verb}",
    )
    message_ts = result["ts"]

    # Now persist the draft with the real Slack message_ts
    temp_draft.slack_message_ts = message_ts
    store.create(temp_draft)

    logger.info(
        "Posted approval message for draft=%s agent=%s type=%s instance=%s verb=%s",
        temp_draft_id,
        agent_name,
        draft_request.capability_type,
        draft_request.capability_instance,
        draft_request.action_verb,
    )

    return temp_draft
