"""Terminal transport adapter — implements ChatAdapter for local dev/E2E use.

A structurally-opposite transport to Slack: threadless, synchronous,
in-process, no Block Kit, no network, no tokens. Exercises all four
ChatAdapter primitives with non-Slack ref encodings.

Adapter-private encoding:
  - ``ConversationRef`` → ``"terminal:<session_id>"``  (not channel:thread)
  - ``PrincipalRef``    → ``"local:<username>"``        (not a Slack user ID)

Core never sees these encodings. Only this file constructs/decodes refs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import TextIO

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

# Wire format for ConversationRef (terminal-private, opaque to core).
_REF_PREFIX = "terminal:"

_STATUS_LABELS: dict[AdapterStatus, str] = {
    AdapterStatus.THINKING: "...",
    AdapterStatus.WORKING: "working",
    AdapterStatus.DONE: "done",
    AdapterStatus.ERROR: "error",
}


def _encode_ref(session_id: str) -> ConversationRef:
    """Build a terminal ``ConversationRef``. Only called from within this adapter."""
    return ConversationRef(f"{_REF_PREFIX}{session_id}")


def _decode_ref(ref: ConversationRef) -> str:
    """Decode a terminal ``ConversationRef``. Only called from within this adapter."""
    return ref.removeprefix(_REF_PREFIX)


class TerminalAdapter(ChatAdapter):
    """Minimal terminal adapter — stdin/stdout, in-process, no network.

    ``supports_interactive=False`` tells core not to call :meth:`prompt_for_choice`
    via the standard flag-gated path (the path Slack always takes via buttons).
    The driver calls :meth:`prompt_for_choice` directly to exercise the
    numbered-text rendering that Slack's button-always path would never reach.
    """

    _CAPABILITIES = AdapterCapabilities(
        supports_threads=False,
        supports_channels=False,
        supports_interactive=False,
    )

    def __init__(
        self,
        username: str = "local-user",
        *,
        output: TextIO | None = None,
        input: TextIO | None = None,
    ) -> None:
        """
        Args:
            username: Local username placed into ``PrincipalRef``.
            output: Writable stream for outbound text (defaults to ``sys.stdout``).
            input:  Readable stream for interactive input (defaults to ``sys.stdin``).
        """
        self._username = username
        self._out: TextIO = output or sys.stdout
        self._in: TextIO = input or sys.stdin
        self._history: list[InboundMessage] = []

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
        """Print the message to the output stream.

        When ``outbound.conversation_ref`` is not ``None`` the session id is
        shown as a prefix so proactive and reply emissions are distinguishable.
        When ``None`` (proactive / cron shape) the text is printed bare.
        """
        if outbound.conversation_ref is not None:
            session_id = _decode_ref(outbound.conversation_ref)
            print(f"[{session_id}] {outbound.text}", file=self._out, flush=True)
        else:
            print(outbound.text, file=self._out, flush=True)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def read_thread(self, conversation_ref: ConversationRef) -> list[InboundMessage]:
        """Return the in-memory message history for this session."""
        return list(self._history)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def set_status(self, conversation_ref: ConversationRef, state: AdapterStatus) -> None:
        """Print a short text status indicator instead of a Slack reaction emoji."""
        label = _STATUS_LABELS.get(state, state.value)
        print(f"[{label}]", file=self._out, flush=True)

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    def resolve_principal(self, raw_user_id: str) -> PrincipalRef:
        """Wrap a local username as a ``PrincipalRef``.

        The ``local:`` prefix makes it structurally distinct from every
        Slack user ID (``U…``) and proves identity-lives-in-transport-config.
        """
        return PrincipalRef(f"local:{raw_user_id}")

    # ------------------------------------------------------------------
    # Mention parsing
    # ------------------------------------------------------------------

    def parse_mentions(self, text: str, conversation_ref: ConversationRef) -> list[str]:
        """Extract ``@agent`` mentions from plain text, in order of appearance."""
        return re.findall(r"@(\w+)", text)

    # ------------------------------------------------------------------
    # Interactive primitive (called by driver, not by core)
    # ------------------------------------------------------------------

    async def prompt_for_choice(
        self,
        conversation_ref: ConversationRef,
        prompt: PromptChoice,
    ) -> StructuredResponse:
        """Render a numbered list to stdout and read a digit reply from stdin.

        Called directly by the terminal driver (not by core, because
        ``supports_interactive=False``). This exercises the numbered-text path
        that Slack's always-interactive button path would never reach.
        """
        print(f"\n{prompt.prompt}", file=self._out, flush=True)
        for i, choice in enumerate(prompt.choices, 1):
            print(f"  {i}. {choice}", file=self._out, flush=True)
        print("Enter number: ", end="", file=self._out, flush=True)

        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, self._in.readline)
        try:
            index = int(raw.strip()) - 1
            if not 0 <= index < len(prompt.choices):
                index = 0
        except ValueError:
            index = 0

        return StructuredResponse(choice=prompt.choices[index], index=index)

    # ------------------------------------------------------------------
    # Adapter-internal helpers
    # ------------------------------------------------------------------

    def record_inbound(self, message: InboundMessage) -> None:
        """Append *message* to the in-memory thread history.

        Called by the driver when it constructs an inbound message so that
        :meth:`read_thread` can return a consistent history. Slack equivalent:
        the Slack API stores history; here the adapter stores it in-process.
        """
        self._history.append(message)


# ---------------------------------------------------------------------------
# Adapter-internal helper — the only place ConversationRef is constructed
# for a terminal session. Core calls this indirectly via the inbound path.
# ---------------------------------------------------------------------------


def make_inbound_ref(session_id: str) -> ConversationRef:
    """Construct a ``ConversationRef`` for a terminal session.

    This is the single construction point for terminal refs. Core never calls
    this directly — it only sees the ref after the adapter creates it.
    The ``terminal:`` prefix makes this structurally distinct from the Slack
    ``channel:thread`` encoding, proving ``ConversationRef`` opacity is real.
    """
    return _encode_ref(session_id)
