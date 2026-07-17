"""Slack transport adapter — implements ChatAdapter over a Bolt WebClient.

Live implementation for the #553 migration: when the ``SLACK_VIA_ADAPTER``
setting is on, ``router.slack_events`` decodes each Bolt event into
:class:`~router.chat.core.InboundFacts` and hands it to
``chat.core.handle_inbound`` with this adapter; when off, the legacy
``_handle_event`` body runs unchanged.

Adapter-private encoding (aligned with TransportRef so command surfaces can
parse it):
  - ``ConversationRef`` → ``"slack:<channel_id>:<thread_ts>"``
  - ``PrincipalRef``    → Slack user ID string (e.g. ``"U01234ABC"``)

Core never sees these encodings. Only this file constructs/decodes refs.

Slack-isms owned here (and nowhere in core): ``md_to_slack`` formatting at
the send boundary, the assistant-thread "is thinking…" status, thread
history via ``conversations.replies``, mention parsing via the runtime
bot-user map, and Guard-2 summary provenance (#547) from message metadata.
"""

from __future__ import annotations

import logging
from typing import Any

from router import runtime
from router.chat.input_collect import collect_input_scripted
from router.chat.interface import ChatAdapter
from router.chat.types import (
    AdapterCapabilities,
    AdapterStatus,
    ConversationRef,
    InboundMessage,
    InputRequest,
    InputResponse,
    OutboundMessage,
    PrincipalRef,
    PromptChoice,
    StructuredResponse,
)
from router.config import get_agent_map
from router.mentions import parse_mentions
from router.slack_format import md_to_slack
from router.slack_users import outbound_mention_ids
from router.thread_loader import HARNESS_SUMMARY_EVENT_TYPE, load_thread_history

logger = logging.getLogger(__name__)

DEFAULT_THINKING_STATUS = "is thinking…"

# Wire format for ConversationRef (Slack-private, opaque to core).
_REF_PREFIX = "slack"
_REF_SEP = ":"

# How many raw thread messages to fetch before core applies its own cap
# (run_agent_turn trims to DEFAULT_MAX_THREAD_MESSAGES) — parity with the
# Discord adapter's history limit.
_READ_THREAD_LIMIT = 100


def _encode_ref(channel_id: str, thread_ts: str) -> ConversationRef:
    """Build a Slack ``ConversationRef``. Only called from within this adapter."""
    return ConversationRef(f"{_REF_PREFIX}{_REF_SEP}{channel_id}{_REF_SEP}{thread_ts}")


def _decode_ref(ref: ConversationRef) -> tuple[str, str]:
    """Decode a Slack ``ConversationRef``. Only called from within this adapter."""
    body = str(ref)
    if body.startswith(f"{_REF_PREFIX}{_REF_SEP}"):
        body = body[len(_REF_PREFIX) + len(_REF_SEP) :]
    channel_id, _, thread_ts = body.partition(_REF_SEP)
    return channel_id, thread_ts


def make_inbound_ref(channel_id: str, thread_ts: str) -> ConversationRef:
    """Construct a ``ConversationRef`` from a Slack event payload.

    The single construction point for Slack refs. Core never calls this
    directly — it only sees the ref after the adapter creates it.
    """
    return _encode_ref(channel_id, thread_ts)


class SlackAdapter(ChatAdapter):
    """One SlackAdapter instance wraps one agent's Bolt WebClient."""

    _CAPABILITIES = AdapterCapabilities(
        supports_threads=True,
        supports_channels=True,
        supports_interactive=True,
    )

    def __init__(self, agent_name: str, client: Any, *, default_channel: str = "") -> None:
        """
        Args:
            agent_name: Logical agent this adapter posts as (thinking-status
                lookup and transcript labeling).
            client: The agent's Bolt ``AsyncWebClient``.
            default_channel: Fallback channel ID used when an outbound
                message carries no ``conversation_ref``.
        """
        self._agent_name = agent_name
        self._client = client
        self._default_channel = default_channel

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._CAPABILITIES

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_message(self, outbound: OutboundMessage) -> None:
        """Post to the thread, converting markdown to Slack mrkdwn at the boundary.

        Plain-text @mentions of known workspace users and persona agents are
        rewritten to real ``<@UID>`` mentions.
        """
        if outbound.conversation_ref is None:
            channel_id, thread_ts = self._default_channel, ""
        else:
            channel_id, thread_ts = _decode_ref(outbound.conversation_ref)
        await self._client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts or None,
            text=md_to_slack(outbound.text, await outbound_mention_ids(self._client)),
        )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def read_thread(self, conversation_ref: ConversationRef) -> list[InboundMessage]:
        """Thread history via ``conversations.replies``, oldest-first.

        Transcript labeling parity with the legacy dispatcher: bot user IDs
        that belong to known agent personas are mapped to the agent's display
        name so the transcript reads "Lisa: …" after handoffs; other senders
        keep their raw Slack user ID.

        Guard 2 (#547): ``is_summary`` is set only from provenance-verified
        harness metadata (``event_type == harness_session_summary``), never
        from marker text alone.
        """
        channel_id, thread_ts = _decode_ref(conversation_ref)
        raw = await load_thread_history(self._client, channel_id, thread_ts, max_messages=_READ_THREAD_LIMIT)

        agent_map = get_agent_map()
        display_by_uid = {
            uid: agent_map.get(name, {}).get("name", name.capitalize())
            for uid, name in runtime.bot_user_map.items()
            if name in agent_map
        }

        messages: list[InboundMessage] = []
        for msg in raw:
            uid = msg.get("user", "") or msg.get("bot_id", "") or ""
            metadata = msg.get("metadata") or {}
            is_summary = metadata.get("event_type") == HARNESS_SUMMARY_EVENT_TYPE
            messages.append(
                InboundMessage(
                    conversation_ref=conversation_ref,
                    principal_ref=PrincipalRef(display_by_uid.get(uid, uid)),
                    text=msg.get("text", "") or "",
                    is_summary=is_summary,
                )
            )
        return messages

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def set_status(self, conversation_ref: ConversationRef, state: AdapterStatus) -> None:
        """THINKING → assistant-thread status; DONE/ERROR → no-op.

        Slack's assistant status auto-clears when the bot posts (or after
        ~2 minutes), so there is nothing to tear down on DONE; error text is
        delivered by the orchestrator through :meth:`send_message`.
        """
        if state is not AdapterStatus.THINKING:
            return
        channel_id, thread_ts = _decode_ref(conversation_ref)
        thinking = get_agent_map().get(self._agent_name, {}).get("thinking_status", DEFAULT_THINKING_STATUS)
        try:
            await self._client.assistant_threads_setStatus(
                channel_id=channel_id,
                thread_ts=thread_ts,
                status=thinking,
            )
        except Exception:
            logger.debug("Could not set assistant status (non-critical)")

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    def resolve_principal(self, raw_user_id: str) -> PrincipalRef:
        """Wrap the Slack user ID as a ``PrincipalRef``."""
        return PrincipalRef(raw_user_id)

    # ------------------------------------------------------------------
    # Mention parsing
    # ------------------------------------------------------------------

    def parse_mentions(self, text: str, conversation_ref: ConversationRef) -> list[str]:
        """Resolve ``<@U…>`` tokens to agent names via the runtime bot-user map."""
        return parse_mentions(text, list(get_agent_map().keys()), dict(runtime.bot_user_map))

    # ------------------------------------------------------------------
    # Interactive primitive
    # ------------------------------------------------------------------

    async def prompt_for_choice(
        self,
        conversation_ref: ConversationRef,
        prompt: PromptChoice,
    ) -> StructuredResponse:
        """Not yet implemented for Slack — approval cards use the Block Kit path.

        Reserved shape per the ChatAdapter contract; returns the first choice
        so type-checking callers stay total. The approval flow proper does not
        ride this method (see docs/chat-backends-architecture.md §3.2).
        """
        logger.debug("SlackAdapter.prompt_for_choice prompt=%r choices=%r", prompt.prompt, prompt.choices)
        return StructuredResponse(choice=prompt.choices[0], index=0)

    # ------------------------------------------------------------------
    # Structured-input primitive (supports_forms=False → scripted fallback)
    # ------------------------------------------------------------------

    async def collect_input(
        self,
        conversation_ref: ConversationRef,
        request: InputRequest,
    ) -> InputResponse:
        """Not yet implemented as a native Slack modal — reserved shape.

        ``supports_forms`` is ``False`` for this adapter, so callers should
        drive :func:`~router.chat.input_collect.collect_input_scripted`
        directly instead of calling this method. It is implemented here (via
        the same scripted helper) purely so the class stays concrete; the
        native ``views.open``/``@app.view`` implementation is tracked in the
        sibling migration issue.
        """
        return await collect_input_scripted(self, conversation_ref, request)
