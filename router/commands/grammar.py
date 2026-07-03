"""Transport-neutral command parser — zero ``slack_sdk`` imports.

The parser consumes normalized message text (the transport marker already
stripped by the per-transport entry shim) and yields a typed :class:`Command`
or ``None``.

Design invariants (all locked in the issue-551 design note):

* **Verb match is case-insensitive**; args are case-preserved.
* **Scope is a static property of the verb** declared here in ``VERB_TABLE``.
  It is never inferred from args at runtime and never conditional on args.
* **``kill`` and ``killall`` are two distinct verbs**.  ``kill`` is
  agent-scoped (the addressed agent's run); ``killall`` is a separate global
  verb (system-wide stop).  ``kill all`` (with a stray ``all`` arg) is NOT
  a global kill — it parses as ``kill`` with a stray arg and the handler
  should emit a usage error.
* **``list packs`` and ``who has`` are exact two-token verbs** — they are
  matched before single-word verbs, and ``list`` or ``who`` alone do not
  match any verb.
* **``approve``/``reject`` are draft-scoped verbs** — subject is the pending
  draft(s) in ``conversation_ref``.  ``yes``/``no`` are marker-free aliases:
  they are recognized without the ``aidt`` prefix **only when**
  ``pending_draft_ids`` is non-empty (i.e. there are pending drafts in the
  current thread).  Outside draft context, bare ``yes``/``no`` returns ``None``
  so normal messages are never swallowed.
* **Unknown verb → ``None``** — the router falls through to normal message
  handling unchanged.
* **Empty text → ``help`` command** — a bare marker is a discoverability
  request, not an error or silence.
* **Zero ``slack_sdk`` import** — transport affordances live in shims only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from router.commands.types import SCOPE_AGENT, SCOPE_DRAFT, SCOPE_GLOBAL, Command


@dataclass(frozen=True)
class VerbEntry:
    """One row in the locked verb table."""

    verb: str
    scope: str
    arg_syntax: str
    gloss: str


VERB_TABLE: Final[tuple[VerbEntry, ...]] = (
    VerbEntry("kill", SCOPE_AGENT, "", "stop the addressed agent's current run"),
    VerbEntry("killall", SCOPE_GLOBAL, "", "system-wide stop: halt every agent"),
    VerbEntry(
        "tasks",
        SCOPE_AGENT,
        "list|create|pause <id>|resume <id>|delete <id>|detail <id>",
        "manage the addressed agent's scheduled tasks",
    ),
    VerbEntry("grant", SCOPE_AGENT, "<pack>", "grant a pack to the addressed agent"),
    VerbEntry("revoke", SCOPE_AGENT, "<pack>", "remove a pack from the addressed agent"),
    VerbEntry("list packs", SCOPE_GLOBAL, "", "list all available packs"),
    VerbEntry("who has", SCOPE_GLOBAL, "<pack>", "show which agents have a pack"),
    VerbEntry("help", SCOPE_GLOBAL, "[<verb>]", "show command list or usage for one verb"),
    VerbEntry("approve", SCOPE_DRAFT, "[<draft-id>]", "approve the pending draft in this thread"),
    VerbEntry("reject", SCOPE_DRAFT, "[<draft-id>]", "reject the pending draft in this thread"),
)

# Fast lookup: canonical verb string → VerbEntry
_VERB_INDEX: Final[dict[str, VerbEntry]] = {e.verb: e for e in VERB_TABLE}

# Partition multi-word from single-word for efficient dispatch
_MULTI_WORD_VERBS: Final[frozenset[str]] = frozenset(e.verb for e in VERB_TABLE if " " in e.verb)
_SINGLE_WORD_VERBS: Final[frozenset[str]] = frozenset(e.verb for e in VERB_TABLE if " " not in e.verb)

# Marker-free aliases for approve/reject: recognized without the ``aidt`` prefix
# only when pending drafts exist in the current conversation thread.
# ``yes`` → ``approve``, ``no`` → ``reject``.
_APPROVAL_ALIASES: Final[dict[str, str]] = {"yes": "approve", "no": "reject"}


# ---------------------------------------------------------------------------
# Help / usage text generation
# ---------------------------------------------------------------------------


def _usage_line(entry: VerbEntry) -> str:
    """One-line usage string for a verb: ``aidt <verb> [<arg-syntax>]``."""
    if entry.arg_syntax:
        return f"aidt {entry.verb} {entry.arg_syntax}"
    return f"aidt {entry.verb}"


def _help_line(entry: VerbEntry) -> str:
    """Full help line: ``aidt <verb> [<arg-syntax>]  — <scope>, <gloss>``."""
    return f"{_usage_line(entry)}  — {entry.scope}, {entry.gloss}"


def help_text(verb: str | None = None) -> str:
    """Return help text generated from :data:`VERB_TABLE` — never hand-written copy.

    * ``verb=None`` → full command list grouped by scope.
    * ``verb="<name>"`` → that verb's usage line (same string the bad-args
      usage error emits, so help and error text can never drift).
    * Unknown ``verb`` → full list (never error on a help lookup).

    Plain text only — rich formatting is a transport capability, not a
    grammar requirement.
    """
    if verb is not None:
        entry = _VERB_INDEX.get(verb.lower())
        if entry is not None:
            return _usage_line(entry)
        # Unknown verb → fall through to full list

    global_entries = [e for e in VERB_TABLE if e.scope == SCOPE_GLOBAL]
    agent_entries = [e for e in VERB_TABLE if e.scope == SCOPE_AGENT]
    draft_entries = [e for e in VERB_TABLE if e.scope == SCOPE_DRAFT]

    lines: list[str] = ["Commands (global — no agent context required):"]
    lines.extend(f"  {_help_line(e)}" for e in global_entries)
    lines.append("Commands (agent-scoped — address an agent first):")
    lines.extend(f"  {_help_line(e)}" for e in agent_entries)
    lines.append("Commands (draft-scoped — act on a pending draft in this thread):")
    lines.extend(f"  {_help_line(e)}" for e in draft_entries)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse(
    text: str,
    *,
    conversation_ref: str | None = None,
    principal_ref: str | None = None,
    transport: str | None = None,
    pending_draft_ids: list[str] | None = None,
) -> Command | None:
    """Parse stripped ``<verb> <args>`` text into a :class:`Command`, or ``None``.

    ``text`` is the message text with the transport marker **already removed**
    by the per-transport entry shim (e.g. the Slack shim strips ``/kill`` →
    ``"kill"``; the ``aidt`` shim strips the keyword → bare verb+args).

    Returns ``None`` when the first token(s) are not a known verb, so the
    router falls through to normal message handling exactly as today (no
    behavior change for non-command messages).

    Empty ``text`` (bare marker with no verb) → ``help`` command.  A lone
    marker is a discoverability request, not silence or an error.

    Verb matching is case-insensitive; args are case-preserved.
    ``subject_ref`` is always ``None`` from the parser — handlers resolve
    the addressed agent from ``conversation_ref`` and populate it.

    ``pending_draft_ids`` enables marker-free approval shortcuts:
    * ``None`` (default) — ``yes``/``no`` are NOT recognized; bare approval
      shortcuts outside a draft thread return ``None`` (no behavior change).
    * Non-empty list — ``yes`` → ``approve``, ``no`` → ``reject``; the handler
      uses these ids for disambiguation.
    * Empty list — ``yes``/``no`` return ``None`` (no pending drafts in thread).
    """
    tokens = (text or "").strip().split()

    if not tokens:
        return Command(
            verb="help",
            args=[],
            scope=SCOPE_GLOBAL,
            conversation_ref=conversation_ref,
            principal_ref=principal_ref,
            transport=transport,
        )

    # Try multi-word verbs first ("list packs", "who has") before single-word
    # so that "list" or "who" alone don't accidentally match a future single-word verb.
    if len(tokens) >= 2:
        two_word = f"{tokens[0].lower()} {tokens[1].lower()}"
        if two_word in _MULTI_WORD_VERBS:
            entry = _VERB_INDEX[two_word]
            return Command(
                verb=entry.verb,
                args=tokens[2:],
                scope=entry.scope,
                conversation_ref=conversation_ref,
                principal_ref=principal_ref,
                transport=transport,
            )

    verb_lower = tokens[0].lower()

    # Check marker-free approval aliases (yes/no) before the main verb table.
    # These are recognized ONLY when pending_draft_ids is non-empty — otherwise
    # they are ordinary text and the parser returns None (no message swallowing).
    canonical = _APPROVAL_ALIASES.get(verb_lower)
    if canonical is not None:
        if pending_draft_ids:
            entry = _VERB_INDEX[canonical]
            return Command(
                verb=entry.verb,
                args=tokens[1:],
                scope=entry.scope,
                conversation_ref=conversation_ref,
                principal_ref=principal_ref,
                transport=transport,
            )
        return None

    if verb_lower in _SINGLE_WORD_VERBS:
        entry = _VERB_INDEX[verb_lower]
        return Command(
            verb=entry.verb,
            args=tokens[1:],
            scope=entry.scope,
            conversation_ref=conversation_ref,
            principal_ref=principal_ref,
            transport=transport,
        )

    return None
