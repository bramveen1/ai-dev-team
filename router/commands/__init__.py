"""Transport-neutral command grammar for the AI dev-team router.

The grammar layer is the single source of truth for which verbs exist, what
scope each has (global vs agent), and what their argument syntax looks like.
It has **zero** ``slack_sdk`` imports — transport affordances live in per-transport
entry shims that strip their native marker and hand bare ``<verb> <args>`` text
to :func:`~router.commands.grammar.parse`.

For Slack-specific entry shimming (stripping slash payloads, ``@mention``, or
the ``aidt`` keyword), see :mod:`router.commands.slack_shim`.

Typical usage in a Slack forwarding shim::

    from router.commands.slack_shim import parse_slack_slash
    from router.kill_command import handle_kill_command_from_parsed

    cmd = parse_slack_slash("/kill", body, conversation_ref=..., principal_ref=...)
    if cmd is not None:
        await handle_kill_command_from_parsed(cmd, ack=ack, body=body, ...)
"""

from router.commands.grammar import VERB_TABLE, VerbEntry, help_text, parse
from router.commands.types import SCOPE_AGENT, SCOPE_GLOBAL, Command, CommandResult, Principal

__all__ = [
    "Command",
    "CommandResult",
    "Principal",
    "SCOPE_AGENT",
    "SCOPE_GLOBAL",
    "VERB_TABLE",
    "VerbEntry",
    "help_text",
    "parse",
]
