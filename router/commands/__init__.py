"""Transport-neutral command grammar for the AI dev-team router.

The grammar layer is the single source of truth for which verbs exist, what
scope each has (global vs agent), and what their argument syntax looks like.
It has **zero** ``slack_sdk`` imports — transport affordances live in per-transport
entry shims that strip their native marker and hand bare ``<verb> <args>`` text
to :func:`~router.commands.grammar.parse`.

Typical usage in a transport shim::

    from router.commands import parse, help_text, Command

    # Slack shim: strip the slash, call the parser
    text = body.get("text", "")          # e.g. "" for /kill, "list" for /tasks list
    verb_text = "kill " + text           # reconstruct "kill <args>"
    cmd = parse(verb_text, transport="slack", ...)
    if cmd is None:
        ...  # not a grammar command — fall through
    elif cmd.verb == "help":
        await respond(text=help_text(cmd.args[0] if cmd.args else None))
    elif cmd.verb == "kill":
        ...  # dispatch to kill handler, resolve subject_ref first
"""

from router.commands.grammar import VERB_TABLE, VerbEntry, help_text, parse
from router.commands.types import SCOPE_AGENT, SCOPE_GLOBAL, Command

__all__ = [
    "Command",
    "SCOPE_AGENT",
    "SCOPE_GLOBAL",
    "VERB_TABLE",
    "VerbEntry",
    "help_text",
    "parse",
]
