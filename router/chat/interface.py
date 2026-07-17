"""Transport-agnostic ChatAdapter interface.

A *transport* is one channel the router is reachable on (Slack, Discord,
WhatsApp, TUI, WebUI). One core, N transports. The core is topology-blind and
identity-blind: it never knows or branches on which transport it is talking
through. Everything Slack-shaped that leaks into core is a future contract
rewrite — this interface exists to stop that.

To add a new transport: subclass :class:`ChatAdapter`, implement every
``abstractmethod``, and declare capabilities via the ``capabilities`` property.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

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


class ChatAdapter(ABC):
    """Contract every transport adapter must satisfy.

    Core routing logic depends only on this interface — never on transport
    primitives (``slack_sdk``, ``discord.py``, etc.). The four primitives
    here are designed as one coherent object and must not be split across
    separate adapters or abstracted away separately.
    """

    # ------------------------------------------------------------------
    # Capability declaration
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Per-adapter capability flags.

        Core behavior degrades on flags, never on transport names. Callers
        check ``self.capabilities.supports_interactive`` before calling
        :meth:`prompt_for_choice`; they must not branch on the adapter class
        or transport name.
        """

    # ------------------------------------------------------------------
    # Outbound — core-initiated emission
    # ------------------------------------------------------------------

    @abstractmethod
    async def send_message(self, outbound: OutboundMessage) -> None:
        """Deliver ``outbound`` to the transport.

        When ``outbound.conversation_ref`` is ``None``, the adapter delivers
        to its configured default destination (a "home" conversation). This
        enables proactive / cron-triggered emission with no inbound to reply
        to — the defining case for why ``conversation_ref`` must be
        persistable.
        """

    # ------------------------------------------------------------------
    # Inbound — read conversation history
    # ------------------------------------------------------------------

    @abstractmethod
    async def read_thread(self, conversation_ref: ConversationRef) -> list[InboundMessage]:
        """Return all messages in the conversation identified by ``conversation_ref``.

        Results are in chronological order. Core treats the ref as an opaque
        token and never inspects its contents; decoding is the adapter's job.
        """

    # ------------------------------------------------------------------
    # Status / typing indicators
    # ------------------------------------------------------------------

    @abstractmethod
    async def set_status(self, conversation_ref: ConversationRef, state: AdapterStatus) -> None:
        """Signal a processing state to the conversation.

        Rich transports (Slack) render reaction emoji; threadless transports
        render a text indicator or no-op gracefully. The ``state`` enum
        ensures non-reaction backends can map each value sensibly instead of
        echoing Slack-specific emoji names.
        """

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    @abstractmethod
    def resolve_principal(self, raw_user_id: str) -> PrincipalRef:
        """Map a transport-native user identifier to an opaque ``PrincipalRef``.

        Identity mapping lives here, in the adapter, not in core. The core
        never sees or branches on raw Slack user IDs, phone numbers, or
        Discord snowflakes — it only ever holds the resulting
        ``PrincipalRef``. Auth resolution happens in core using only that
        opaque value; the agent is auth-blind.
        """

    # ------------------------------------------------------------------
    # Mention parsing
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_mentions(self, text: str, conversation_ref: ConversationRef) -> list[str]:
        """Extract agent-name mentions from ``text`` in the context of the conversation.

        Returns logical agent names in order of first appearance. The
        implementation may consult transport-specific mention formats (e.g.
        ``<@U…>`` in Slack) and the adapter's own bot-user map.
        """

    # ------------------------------------------------------------------
    # Interactive primitive (gated by supports_interactive)
    # ------------------------------------------------------------------

    @abstractmethod
    async def prompt_for_choice(
        self,
        conversation_ref: ConversationRef,
        prompt: PromptChoice,
    ) -> StructuredResponse:
        """Present an interactive choice prompt and return the user's selection.

        Only call this when ``self.capabilities.supports_interactive`` is
        ``True``. Rich transports render buttons / Block Kit; threadless
        transports render a numbered text list and accept a digit reply. The
        de-Slacking *implementation* is tracked separately — this method
        reserves the *shape* so that phase has something to build against.

        Args:
            conversation_ref: Where to post the prompt.
            prompt: The question and enumerated choices.

        Returns:
            The user's selected :class:`~router.chat.types.StructuredResponse`.
        """

    # ------------------------------------------------------------------
    # Structured-input primitive (gated by supports_forms)
    # ------------------------------------------------------------------

    @abstractmethod
    async def collect_input(
        self,
        conversation_ref: ConversationRef,
        request: InputRequest,
    ) -> InputResponse:
        """Present a multi-field form and return the user's structured answers.

        Only call this when ``self.capabilities.supports_forms`` is ``True``.
        Rich transports render a native form (e.g. a Slack modal); when the
        capability is absent, callers should drive
        :func:`router.chat.input_collect.collect_input_scripted` instead of
        calling this method — that helper fulfils an ``InputRequest`` over
        any adapter's ``send_message``/``read_thread`` primitives with no
        native-form support required. This method reserves the *shape* for
        transports that do gain native support, mirroring how
        :meth:`prompt_for_choice` reserves the choice-prompt shape.

        Args:
            conversation_ref: Where to post the form.
            request: The form's title, fields, and timeout.

        Returns:
            The user's answers as an :class:`~router.chat.types.InputResponse`.
        """
