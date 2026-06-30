"""Slack-specific entry shim for the transport-neutral command grammar.

Strips the Slack-native affordance (slash payload, ``@mention`` prefix, or
the ``aidt`` keyword) before handing bare ``<verb> <args>`` text to the
grammar parser.

This is the **only** module that knows about Slack affordances in the
command-routing path.  The grammar parser (``router.commands.grammar``) and
all handlers have zero ``slack_sdk`` imports.

Canonical flows
---------------

Slack slash command (``/kill``, ``/tasks``, ``/killall``, …)::

    parse_slack_slash("/kill", body, conversation_ref=..., principal_ref=...)
        → parse("kill", transport="slack", ...)
        → Command(verb="kill", scope=SCOPE_AGENT, ...)

Bot ``@mention`` or ``aidt`` keyword in a message::

    parse_from_message("<@U123> kill", conversation_ref=..., principal_ref=...)
        → strip_mention → parse("kill", transport="slack", ...)
        → Command(verb="kill", scope=SCOPE_AGENT, ...)
"""

from __future__ import annotations

import re

from router.commands.grammar import parse
from router.commands.types import Command

_MENTION_RE = re.compile(r"^<@[A-Z0-9]+>\s*", re.IGNORECASE)
_AIDT_RE = re.compile(r"^aidt\s+", re.IGNORECASE)
_AIDT_BARE_RE = re.compile(r"^aidt$", re.IGNORECASE)

# Slash body tokens that signal a fleet-wide kill (old ``/kill all`` form).
# In the new grammar these map to the ``killall`` verb.
_KILLALL_TOKENS: frozenset[str] = frozenset({"all", "*", "everywhere"})

# Optional dev/staging prefixes that may be prepended to command names.
_DEV_PREFIX_RE = re.compile(r"^(?:dev|staging)-")


def strip_mention(text: str) -> str:
    """Remove a leading ``<@USER>`` Slack mention, preserving the rest."""
    return _MENTION_RE.sub("", (text or "")).strip()


def strip_aidt(text: str) -> str:
    """Remove a leading ``aidt`` keyword, returning bare ``<verb> <args>``."""
    stripped = (text or "").strip()
    if _AIDT_BARE_RE.match(stripped):
        return ""
    return _AIDT_RE.sub("", stripped).strip()


def _slash_kill_body_to_verb_text(body_text: str) -> str:
    """Map ``/kill`` slash body text to the bare verb text for the parser.

    ``/kill`` (no args)              → ``"kill"``   (agent-scoped; handler resolves)
    ``/kill all`` / ``/kill *``      → ``"killall"`` (global fleet-wide stop)
    ``/kill sam``                    → ``"kill sam"`` (legacy positional; handler uses arg[0])
    """
    parts = (body_text or "").strip().split()
    if not parts:
        return "kill"
    if parts[0].lower() in _KILLALL_TOKENS:
        return "killall"
    return "kill " + " ".join(parts)


def parse_slack_slash(
    command_name: str,
    body: dict,
    *,
    conversation_ref: str | None = None,
    principal_ref: str | None = None,
) -> Command | None:
    """Parse a Slack slash command body into a ``Command``, or ``None``.

    ``command_name`` is the full slash command string (e.g. ``"/kill"``).
    ``body`` is the raw Slack slash command payload dict.

    Returns ``None`` only if the verb produced by the shim mapping is not in
    the grammar table — which should never happen for registered commands but
    is handled defensively.
    """
    raw_name = command_name.lstrip("/").lower()
    # Strip optional dev/staging prefix (e.g. "dev-kill" → "kill").
    verb_name = _DEV_PREFIX_RE.sub("", raw_name)

    body_text = (body.get("text") or "").strip()

    if verb_name == "kill":
        verb_text = _slash_kill_body_to_verb_text(body_text)
    elif verb_name == "killall":
        verb_text = "killall"
    elif verb_name == "tasks":
        verb_text = f"tasks {body_text}".strip()
    else:
        verb_text = f"{verb_name} {body_text}".strip()

    return parse(
        verb_text,
        conversation_ref=conversation_ref,
        principal_ref=principal_ref,
        transport="slack",
    )


def parse_from_message(
    text: str,
    *,
    conversation_ref: str | None = None,
    principal_ref: str | None = None,
) -> Command | None:
    """Parse a Slack message text (mention or ``aidt`` keyword) into a ``Command``.

    Strips the ``<@USER>`` mention prefix or ``aidt`` keyword, then delegates
    to the grammar parser.  Returns ``None`` when the remaining text does not
    start with a known verb — normal messages fall through to agent dispatch.
    """
    stripped = strip_mention(text)
    if _AIDT_RE.match(stripped) or _AIDT_BARE_RE.match(stripped):
        stripped = strip_aidt(stripped)
    return parse(
        stripped,
        conversation_ref=conversation_ref,
        principal_ref=principal_ref,
        transport="slack",
    )
