"""Response interceptor for the approval flow.

Parses agent responses for ``draft-approval`` code-fence blocks,
creates Draft records, resolves pack-aware buttons, and posts Block
Kit approval messages to Slack.

Agents include a structured ``draft-approval`` JSON block in their
response when they create a draft via an MCP tool. The interceptor
strips that block from the visible response and routes it through the
approval flow.

Schema:

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

# Regex to match ``` draft-approval ... ``` fenced blocks.
#
# We also accept ``dispatch-approval`` as an alias: agents (notably
# ``sam``) repeatedly emit the dispatch pack's verb name as the fence
# info string and splat the handler's raw ``approval_required`` response
# verbatim — see the fabrication-pattern memory entries and the multiple
# Slack incidents where no card appeared because the fence didn't match.
# Treating it as an alias plus the schema-translation step below
# (``_translate_handler_native_shape``) turns the most common silent
# failure into a working card.  When/if agents stop making this mistake
# the alias can be removed without breaking anything: the canonical
# fence is unchanged.
_DRAFT_BLOCK_RE = re.compile(
    r"```(?:draft-approval|dispatch-approval)\s*\n(.*?)\n```",
    re.DOTALL,
)

# Always-required fields in any draft-approval block, old schema or new.
_ALWAYS_REQUIRED = {"draft_id", "action_verb", "payload"}

# Pre-render payload validation (issue #265, drift mode #2).
#
# A block can parse cleanly and carry every top-level key yet still be
# unfulfillable because ``payload`` is missing a field the executor reads
# at click time.  Rendering a card the executor cannot fulfill is worse
# than refusing to render: the click is the human's commitment and we
# should not invite it when we already know it will fail.
#
# Keyed by ``action_verb`` → set of payload keys that must be present and
# truthy.  ``dispatch_issue`` reads ``payload.issue_url`` in
# ``router/app.py`` ``_execute_approved_draft`` (the executor bails with
# "has no issue_url in payload" otherwise).  Keep this in sync with the
# fields each executor actually dereferences.
_REQUIRED_PAYLOAD_FIELDS: dict[str, set[str]] = {
    "dispatch_issue": {"issue_url"},
}


def _translate_handler_native_shape(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the dispatch handler's raw ``approval_required`` shape to canonical.

    The dispatch pack's handler returns ::

        {"status": "approval_required", "draft_id": "...", "preview": {...}}

    The canonical ``draft-approval`` schema is ::

        {"draft_id": "...", "action_verb": "dispatch_issue",
         "payload": {...}, "pack": "dispatch"}

    Agents have been repeatedly observed splatting the handler response
    directly into the fence rather than re-formatting it.  When we detect
    that exact shape, we map it to canonical so the card still posts.

    Returns the input unchanged if it doesn't match the handler-native
    shape — so canonical inputs flow straight through.
    """
    if (
        data.get("status") == "approval_required"
        and "preview" in data
        and "action_verb" not in data
        and "payload" not in data
    ):
        preview = data.get("preview")
        if isinstance(preview, dict):
            return {
                "draft_id": data.get("draft_id"),
                "action_verb": "dispatch_issue",
                "payload": preview,
                "pack": "dispatch",
            }
    return data


# Phrases that strongly suggest an agent claimed to create a draft / dispatch.
# Kept tight on purpose: we want concrete signals (an actual draft_id hex,
# explicit "queued the dispatch" phrasing, the dispatch rocket emoji prefix)
# rather than any mention of the word "draft". Tuned to flag the specific
# fabrication pattern we've seen — agents narrating "draft_id: <hex>" or
# "approval card incoming" without ever emitting the ```draft-approval block.
_CLAIM_PHRASES_RE = re.compile(
    r"(?:\bdraft_id[:\s]+[a-f0-9-]{6,}"  # "draft_id: fad268bc"
    r"|approval card (?:incoming|landing|en route)"
    r"|queued (?:it|the dispatch|a sonnet)"
    r"|:rocket:\s*dispatch\s+`)",
    re.IGNORECASE,
)


def detect_unbacked_claim(text: str, has_drafts: bool) -> str | None:
    """Detect agent prose that claims a draft without an accompanying block.

    Returns the offending phrase (for logging) when ``text`` matches a
    known "I queued / drafted / dispatched" pattern but ``has_drafts``
    is False — meaning the agent narrated a draft into existence without
    emitting the ``draft-approval`` fenced block, so no card was posted.

    Returns ``None`` when ``has_drafts`` is True (the block is real) or
    when no claim-like phrase is present.

    This is a safety-net guard, not a substitute for system-prompt
    discipline. It catches the lie after the fact so callers can attach
    a visible warning rather than silently shipping misleading prose.
    """
    if has_drafts:
        return None
    m = _CLAIM_PHRASES_RE.search(text)
    return m.group(0).strip() if m else None


@dataclass
class DraftRequest:
    """A parsed draft approval request extracted from agent response text.

    Exactly one of ``pack`` (for pack-backed drafts) or ``target`` (for
    connector-backed deep-link drafts) must be set.
    """

    draft_id: str
    action_verb: str
    payload: dict[str, Any]
    pack: str | None = None
    target: str | None = None


@dataclass
class ParseError:
    """A ``draft-approval`` block that failed to parse or validate.

    Previously these failures were logged and the block silently stripped,
    so the thread read as if a card had posted when none had (issue #265).
    Carrying the failure out of ``parse_response`` lets the caller post a
    single thread-visible message so the human sees what went wrong and the
    agent sees its own error on re-entry and can self-correct.

    ``kind`` is a machine label (``malformed_json``, ``missing_fields``,
    ``missing_routing``, ``ambiguous_routing``, ``missing_payload_field``);
    ``summary`` is a human sentence naming the specific problem; ``raw_block``
    is the (truncated) original fence body for the quoted context.
    """

    kind: str
    summary: str
    raw_block: str = ""

    def to_slack_message(self, agent_name: str) -> str:
        """Render a thread-visible Slack message describing the failure."""
        name = agent_name.capitalize() if agent_name else "Agent"
        lines = [f":warning: *{name}'s* `draft-approval` block was not posted — {self.summary}"]
        if self.raw_block:
            quoted = self.raw_block if len(self.raw_block) <= 500 else f"{self.raw_block[:500]} …"
            lines.append(f"```\n{quoted}\n```")
        return "\n".join(lines)


@dataclass
class InterceptResult:
    """Result of parsing an agent response for draft-approval blocks."""

    cleaned_text: str
    draft_requests: list[DraftRequest] = field(default_factory=list)
    parse_errors: list[ParseError] = field(default_factory=list)

    @property
    def has_drafts(self) -> bool:
        return len(self.draft_requests) > 0

    @property
    def has_parse_errors(self) -> bool:
        return len(self.parse_errors) > 0


def parse_response(response_text: str) -> InterceptResult:
    """Parse an agent response for draft-approval code-fence blocks.

    Extracts all ``draft-approval`` blocks, validates the JSON, and
    returns an InterceptResult with the cleaned text and parsed
    requests. Malformed or unfulfillable blocks are stripped, but the
    failure is recorded in ``parse_errors`` so the caller can post a
    thread-visible message rather than dropping the block silently
    (issue #265).
    """
    draft_requests: list[DraftRequest] = []
    parse_errors: list[ParseError] = []

    def _replace_block(match: re.Match) -> str:
        raw_json = match.group(1).strip()
        truncated = raw_json[:200]
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("Malformed JSON in draft-approval block: %s", truncated)
            parse_errors.append(
                ParseError(
                    kind="malformed_json",
                    summary=f"the block is not valid JSON ({exc.msg})",
                    raw_block=truncated,
                )
            )
            return ""

        # Tolerate the dispatch handler's raw output shape.  This is a
        # narrow, schema-pinned translation — only the exact
        # ``{status: approval_required, draft_id, preview}`` triple
        # triggers it.  See ``_translate_handler_native_shape``.
        if isinstance(data, dict):
            translated = _translate_handler_native_shape(data)
            if translated is not data:
                logger.info(
                    "Translated handler-native approval shape to canonical (draft_id=%s)",
                    data.get("draft_id"),
                )
                data = translated

        if not isinstance(data, dict):
            logger.warning("draft-approval block is not a JSON object: %s", truncated)
            parse_errors.append(
                ParseError(
                    kind="malformed_json",
                    summary="the block is not a JSON object",
                    raw_block=truncated,
                )
            )
            return ""

        missing = _ALWAYS_REQUIRED - set(data.keys())
        if missing:
            fields = ", ".join(f"`{f}`" for f in sorted(missing))
            logger.warning("draft-approval block missing fields %s: %s", missing, truncated)
            parse_errors.append(
                ParseError(
                    kind="missing_fields",
                    summary=f"it is missing required field(s): {fields}",
                    raw_block=truncated,
                )
            )
            return ""

        has_pack = bool(data.get("pack"))
        has_target = bool(data.get("target"))

        if not (has_pack or has_target):
            logger.warning(
                "draft-approval block needs exactly one of {pack, target}: %s",
                truncated,
            )
            parse_errors.append(
                ParseError(
                    kind="missing_routing",
                    summary="it must set exactly one of `pack` or `target`",
                    raw_block=truncated,
                )
            )
            return ""
        if has_pack and has_target:
            logger.warning(
                "draft-approval block sets both pack and target — choose one: %s",
                truncated,
            )
            parse_errors.append(
                ParseError(
                    kind="ambiguous_routing",
                    summary="it sets both `pack` and `target` — choose exactly one",
                    raw_block=truncated,
                )
            )
            return ""

        payload = data["payload"] if isinstance(data["payload"], dict) else {}
        action_verb = str(data["action_verb"])

        # Pre-render payload validation (issue #265, drift mode #2). Refuse
        # to render a card the executor cannot fulfill at click time. We use
        # ``not payload.get(f)`` (not just key-presence) so an empty/blank
        # ``issue_url`` is caught too — that mirrors the executor's own
        # ``if not issue_url`` bail in ``_execute_approved_draft``.
        required_payload = _REQUIRED_PAYLOAD_FIELDS.get(action_verb, set())
        missing_payload = sorted(f for f in required_payload if not payload.get(f))
        if missing_payload:
            fields = ", ".join(f"`payload.{f}`" for f in missing_payload)
            logger.warning(
                "draft-approval %s block missing executor-required payload field(s) %s: %s",
                action_verb,
                missing_payload,
                truncated,
            )
            parse_errors.append(
                ParseError(
                    kind="missing_payload_field",
                    summary=f"the `{action_verb}` payload is missing required field(s): {fields} — card not rendered",
                    raw_block=truncated,
                )
            )
            return ""

        draft_requests.append(
            DraftRequest(
                draft_id=str(data["draft_id"]),
                action_verb=action_verb,
                payload=payload,
                pack=str(data["pack"]) if has_pack else None,
                target=str(data["target"]) if has_target else None,
            )
        )
        return ""

    cleaned = _DRAFT_BLOCK_RE.sub(_replace_block, response_text).strip()
    return InterceptResult(
        cleaned_text=cleaned,
        draft_requests=draft_requests,
        parse_errors=parse_errors,
    )


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
    """
    if pack is not None:
        return ("pack", pack.name)
    # Validated upstream: at this point target is always set when pack is None.
    return ("connector", draft_request.target or "")


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
