"""Transport-agnostic type definitions for the ChatAdapter contract.

These types form the boundary between core routing logic and transport-specific
adapters. The ``ConversationRef`` and ``PrincipalRef`` types are opaque to core:
core code may store and echo them but must never construct or parse them. Only
adapter code (in ``router/chat/adapters/``) is allowed to call the ``NewType``
constructors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal, NewType

# ---------------------------------------------------------------------------
# Opaque reference types
# ---------------------------------------------------------------------------

# Addressing token created by an adapter on inbound; echoed by core on reply.
# Persistable as a plain string so the core can store a ref and emit to it
# later (e.g. cron ticks, "PR merged" events — no inbound to reply to).
ConversationRef = NewType("ConversationRef", str)

# Identity token created by the adapter from its native user ID (Slack UID,
# phone number, Discord snowflake, …). Core never constructs or parses it;
# auth resolution happens in the core using only this opaque value.
PrincipalRef = NewType("PrincipalRef", str)


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class AdapterStatus(str, Enum):
    """Generic processing-state enum for :meth:`ChatAdapter.set_status`.

    Using an enum (not free-form strings) lets non-reaction backends map each
    state to a sensible text indicator rather than echoing Slack-specific emoji.
    """

    THINKING = "thinking"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterCapabilities:
    """Per-adapter capability declaration.

    Core behavior degrades based on these flags — never on transport names.
    No ``if transport == "slack"`` branching is permitted in core.
    """

    supports_threads: bool = False
    supports_channels: bool = False
    supports_interactive: bool = False
    supports_forms: bool = False


# ---------------------------------------------------------------------------
# Message shapes
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    """A message received from a transport — core-facing.

    Both refs are opaque to core. The adapter constructs them; core echoes
    ``conversation_ref`` on reply and passes ``principal_ref`` to auth.
    """

    conversation_ref: ConversationRef
    principal_ref: PrincipalRef
    text: str
    attachments: list[str] = field(default_factory=list)
    # Provenance flag for harness-authored session summaries (issue #547
    # Guard 2). Only the adapter may set this, and only for messages it can
    # verify were authored by its own bot identity — core uses it to resume
    # from a session summary without trusting marker text from arbitrary
    # senders.
    is_summary: bool = False


@dataclass
class OutboundMessage:
    """A message the core emits to a transport.

    ``conversation_ref`` is the stored addressing token returned by a prior
    inbound. When ``None``, the adapter falls back to its configured default
    destination (a "home" conversation per transport). This enables proactive /
    cron-triggered emission with no inbound to reply to.
    """

    text: str
    conversation_ref: ConversationRef | None = None


# ---------------------------------------------------------------------------
# Interactive primitive shapes
# ---------------------------------------------------------------------------


@dataclass
class PromptChoice:
    """Abstract interactive prompt — a question with enumerated options.

    Rich transports (Slack) render this as buttons / Block Kit. Threadless
    transports render a numbered text list and accept a digit reply.
    Only used when ``AdapterCapabilities.supports_interactive`` is ``True``.
    """

    prompt: str
    choices: list[str]
    timeout_seconds: int = 60


@dataclass
class StructuredResponse:
    """Result of an :meth:`ChatAdapter.prompt_for_choice` call."""

    choice: str
    index: int


# ---------------------------------------------------------------------------
# Structured-input (multi-field form) primitive shapes
# ---------------------------------------------------------------------------


class InputFieldType(str, Enum):
    """Field kinds supported by :class:`InputField`.

    Deliberately small: real collectors (create-task's name/prompt/cron/
    destination/timeout, pack-grant's PAT) all reduce to these four. ``CHOICE``
    covers enumerated pickers; ``validator`` on the field covers everything
    more specific (cron strings, bounded ints) without a bespoke type.
    """

    TEXT = "text"
    SECRET = "secret"
    CHOICE = "choice"
    INT = "int"


@dataclass
class InputField:
    """One field in a :class:`InputRequest` form.

    ``validator`` may be a callable (``str -> bool``, raising is treated as
    invalid rather than crashing the collector) or a compiled regex, matched
    with ``fullmatch``. ``options`` is required when ``type`` is ``CHOICE``.
    """

    key: str
    label: str
    type: InputFieldType = InputFieldType.TEXT
    required: bool = True
    options: list[str] | None = None
    validator: Callable[[str], bool] | re.Pattern[str] | None = None


@dataclass
class InputRequest:
    """Abstract multi-field form — the ``PromptChoice`` sibling for structured input.

    Rich transports (a future Slack modal) render this as a native form.
    Threadless / scripted transports fulfil it via
    :func:`router.chat.input_collect.collect_input_scripted`, asking one
    field per message. Only used when ``AdapterCapabilities.supports_forms``
    is ``True``, or via the scripted fallback when it is not.
    """

    title: str
    fields: list[InputField]
    timeout_seconds: int = 300


@dataclass
class InputResponse:
    """Result of an :meth:`ChatAdapter.collect_input` call.

    ``values`` maps :attr:`InputField.key` to the collected (and validated)
    string value; fields not reached before cancellation/timeout are absent.
    """

    values: dict[str, str]
    status: Literal["completed", "cancelled", "timed_out"]
