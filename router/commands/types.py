"""Core types for the transport-neutral command grammar.

All fields on :class:`Command` are set by the parser except ``subject_ref``,
which is populated by the handler after resolving the addressed agent from
``conversation_ref`` and the transport's mention/addressing context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SCOPE_GLOBAL = "global"
SCOPE_AGENT = "agent"
SCOPE_DRAFT = "draft"


@dataclass
class Command:
    """A parsed, structured command ready for handler dispatch.

    Produced by :func:`router.commands.grammar.parse`. ``subject_ref`` is
    always ``None`` from the parser — handlers resolve the addressed agent
    from ``conversation_ref`` and populate it before dispatch.

    Scope is a static property of the verb (declared in the grammar table),
    never inferred from args at runtime.
    """

    verb: str
    args: list[str] = field(default_factory=list)
    scope: str = SCOPE_GLOBAL  # SCOPE_GLOBAL | SCOPE_AGENT | SCOPE_DRAFT
    subject_ref: str | None = None
    conversation_ref: str | None = None
    principal_ref: str | None = None
    transport: str | None = None


@dataclass(frozen=True)
class Principal:
    """The entity that issued a command, with its human-or-bot classification.

    ``ref`` is the transport-native principal identifier (e.g.
    ``"discord:123456789"`` or ``"slack:U_op"``).  ``kind`` is set by the
    per-transport entry shim from transport-native signals — for Discord this
    is ``message.author.bot``; for Slack it is the user type on the payload.
    """

    ref: str
    kind: Literal["human", "bot"]


@dataclass
class CommandResult:
    """Transport-neutral result returned by every verb handler.

    ``text`` is plain prose suitable for any transport to render without
    further transformation.  ``ok`` is ``False`` when the command was
    rejected or failed (human-only gate, unknown agent, etc.).
    """

    text: str
    ok: bool = True
