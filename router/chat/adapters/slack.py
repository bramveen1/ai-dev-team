"""Slack transport stub — implements ChatAdapter for type-checking only.

This stub proves the interface compiles and type-checks against a real
Slack-flavored implementation. It is **not** wired into the live Slack path;
that behavior-preserving migration is tracked in the sibling issue.

Adapter-private encoding:
  - ``ConversationRef`` → ``"<channel_id>:<thread_ts>"``
  - ``PrincipalRef``    → Slack user ID string (e.g. ``"U01234ABC"``)

Core never sees these encodings. Only this file constructs/decodes refs.
"""

from __future__ import annotations

import logging

from router.chat.interface import ChatAdapter
from router.chat.types import (
    AdapterCapabilities,
    AdapterStatus,
    ConversationRef,
    InboundMessage,
    OutboundMessage,
    PrincipalRef,
    PromptChoice,
    StructuredResponse,
)

logger = logging.getLogger(__name__)

# Wire format for ConversationRef (Slack-private, opaque to core).
_REF_SEP = ":"


def _encode_ref(channel_id: str, thread_ts: str) -> ConversationRef:
    """Build a Slack ``ConversationRef``. Only called from within this adapter."""
    return ConversationRef(f"{channel_id}{_REF_SEP}{thread_ts}")


def _decode_ref(ref: ConversationRef) -> tuple[str, str]:
    """Decode a Slack ``ConversationRef``. Only called from within this adapter."""
    channel_id, _, thread_ts = ref.partition(_REF_SEP)
    return channel_id, thread_ts


class SlackAdapter(ChatAdapter):
    """Minimal Slack adapter stub — compiles and type-checks against ChatAdapter.

    No live Slack calls are made. All async methods are stubs that log and
    return safe defaults. The :func:`make_inbound_ref` helper demonstrates
    the only place where ``ConversationRef`` construction is allowed.
    """

    _CAPABILITIES = AdapterCapabilities(
        supports_threads=True,
        supports_channels=True,
        supports_interactive=True,
    )

    def __init__(self, default_channel: str = "") -> None:
        """
        Args:
            default_channel: Fallback Slack channel ID used when an outbound
                message carries no ``conversation_ref``.
        """
        self._default_channel = default_channel

    # ------------------------------------------------------------------
    # Capability declaration
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._CAPABILITIES

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_message(self, outbound: OutboundMessage) -> None:
        """Stub: log the target channel/thread; make no Slack API call."""
        if outbound.conversation_ref is None:
            channel_id, thread_ts = self._default_channel, ""
        else:
            channel_id, thread_ts = _decode_ref(outbound.conversation_ref)
        logger.debug("SlackAdapter.send_message channel=%s thread=%s", channel_id, thread_ts)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def read_thread(self, conversation_ref: ConversationRef) -> list[InboundMessage]:
        """Stub: log the target thread; return an empty history."""
        channel_id, thread_ts = _decode_ref(conversation_ref)
        logger.debug("SlackAdapter.read_thread channel=%s thread=%s", channel_id, thread_ts)
        return []

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def set_status(self, conversation_ref: ConversationRef, state: AdapterStatus) -> None:
        """Stub: log the status transition; add no reaction emoji."""
        channel_id, _ = _decode_ref(conversation_ref)
        logger.debug("SlackAdapter.set_status channel=%s state=%s", channel_id, state)

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    def resolve_principal(self, raw_user_id: str) -> PrincipalRef:
        """Wrap the Slack user ID as a ``PrincipalRef``.

        The real implementation would validate the ID against workspace
        membership and apply any identity-mapping rules from the adapter
        config. The stub wraps as-is.
        """
        return PrincipalRef(raw_user_id)

    # ------------------------------------------------------------------
    # Mention parsing
    # ------------------------------------------------------------------

    def parse_mentions(self, text: str, conversation_ref: ConversationRef) -> list[str]:
        """Stub: return no mentions.

        The real implementation delegates to ``router.mentions.parse_mentions``
        with the bot-user map loaded from this adapter's Slack credentials.
        """
        return []

    # ------------------------------------------------------------------
    # Interactive primitive
    # ------------------------------------------------------------------

    async def prompt_for_choice(
        self,
        conversation_ref: ConversationRef,
        prompt: PromptChoice,
    ) -> StructuredResponse:
        """Stub: log the prompt; return the first choice as a default.

        The real implementation renders Slack Block Kit buttons, waits for an
        ``block_actions`` event, and returns the user's selection.
        """
        logger.debug("SlackAdapter.prompt_for_choice prompt=%r choices=%r", prompt.prompt, prompt.choices)
        return StructuredResponse(choice=prompt.choices[0], index=0)


# ---------------------------------------------------------------------------
# Adapter-internal helper — the only place ConversationRef is constructed
# for a Slack event. Core calls this indirectly via the inbound path.
# ---------------------------------------------------------------------------


def make_inbound_ref(channel_id: str, thread_ts: str) -> ConversationRef:
    """Construct a ``ConversationRef`` from a Slack event payload.

    This is the single construction point for Slack refs. Core never calls
    this directly — it only sees the ref after the adapter creates it.
    """
    return _encode_ref(channel_id, thread_ts)
