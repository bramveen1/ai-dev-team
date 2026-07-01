"""Discord-specific entry shim for the transport-neutral command grammar.

Strips the Discord-native affordance (``@Agent aidt <verb> <args>``) before
handing bare ``<verb> <args>`` text to the grammar parser.

This is the **only** module that knows about Discord affordances in the
command-routing path.  The grammar parser (``router.commands.grammar``) and
all verb handlers have zero Discord-SDK imports.

Canonical flows
---------------

Bot ``@mention`` + ``aidt`` keyword in a Discord message::

    parse_from_discord_message("<@SNOWFLAKE> aidt kill", conversation_ref=..., principal_ref=...)
        → strip mention → strip aidt → parse("kill", transport="discord", ...)
        → Command(verb="kill", scope=SCOPE_AGENT, transport="discord", ...)

Bare ``aidt`` keyword (no @mention, e.g. from a thread follow-up)::

    parse_from_discord_message("aidt killall", ...)
        → strip aidt → parse("killall", transport="discord", ...)
        → Command(verb="killall", scope=SCOPE_GLOBAL, ...)

Bare ``@mention aidt`` with no verb::

    parse_from_discord_message("<@SNOWFLAKE> aidt", ...)
        → bare marker → parse("", transport="discord", ...)
        → Command(verb="help", ...)

Non-aidt message::

    parse_from_discord_message("hey, can you review this PR?", ...)
        → None  (router falls through to normal agent dispatch)
"""

from __future__ import annotations

import re

from router.commands.grammar import parse
from router.commands.types import Command

# Strips a Discord snowflake mention (<@123456> or <@!123456>) from the start
# of the text.  The ``!`` is the old "nickname mention" form that older clients
# still emit.
_MENTION_RE = re.compile(r"^<@!?\d+>\s*", re.IGNORECASE)

# Matches a leading ``aidt`` keyword followed by at least one whitespace char.
_AIDT_RE = re.compile(r"^aidt\s+", re.IGNORECASE)

# Matches the bare ``aidt`` keyword with nothing after it (discoverability).
_AIDT_BARE_RE = re.compile(r"^aidt$", re.IGNORECASE)


def _strip_mention(text: str) -> str:
    """Remove a leading Discord snowflake mention, preserving the rest."""
    return _MENTION_RE.sub("", (text or "")).strip()


def _strip_aidt(text: str) -> str:
    """Remove a leading ``aidt`` keyword, returning bare ``<verb> <args>``."""
    stripped = (text or "").strip()
    if _AIDT_BARE_RE.match(stripped):
        return ""
    return _AIDT_RE.sub("", stripped).strip()


def parse_from_discord_message(
    text: str,
    *,
    conversation_ref: str | None = None,
    principal_ref: str | None = None,
) -> Command | None:
    """Parse a Discord message text into a :class:`~router.commands.types.Command`.

    Accepts messages in any of these forms:

    * ``@Agent aidt <verb> <args>``   — @mention + ``aidt`` keyword
    * ``@Agent aidt``                 — bare, yields ``help``
    * ``aidt <verb> <args>``          — no @mention (e.g. thread follow-up)
    * ``aidt``                        — bare, yields ``help``

    Returns ``None`` when the message does not contain the ``aidt`` keyword
    (after stripping any leading @mention) — the router should fall through to
    normal agent dispatch for those messages.

    The ``aidt`` match is **case-insensitive** (``AIDT``, ``Aidt`` all work).
    """
    # Strip any leading Discord snowflake mention first.
    after_mention = _strip_mention(text)

    # The message must contain the ``aidt`` keyword at this position.
    if not (_AIDT_RE.match(after_mention) or _AIDT_BARE_RE.match(after_mention)):
        return None

    bare = _strip_aidt(after_mention)

    return parse(
        bare,
        conversation_ref=conversation_ref,
        principal_ref=principal_ref,
        transport="discord",
    )
