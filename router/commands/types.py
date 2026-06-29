"""Core types for the transport-neutral command grammar.

All fields on :class:`Command` are set by the parser except ``subject_ref``,
which is populated by the handler after resolving the addressed agent from
``conversation_ref`` and the transport's mention/addressing context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCOPE_GLOBAL = "global"
SCOPE_AGENT = "agent"


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
    scope: str = SCOPE_GLOBAL  # SCOPE_GLOBAL | SCOPE_AGENT
    subject_ref: str | None = None
    conversation_ref: str | None = None
    principal_ref: str | None = None
    transport: str | None = None
