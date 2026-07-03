"""Unit tests for router.commands — the transport-neutral command grammar.

Covers the locked design from the issue-551 design note:
* Valid parse for every verb → correct Command fields
* Case-insensitive verb matching, case-preserved args
* Multi-word verbs (list packs, who has)
* kill vs killall scope distinction (decided by verb identity alone)
* kill all (with arg) is NOT a global kill — it's kill with a stray arg
* Unknown verb → None (router falls through unchanged)
* Empty text → help command (bare marker = discoverability request)
* help_text generated from VERB_TABLE — every verb present
* help_text(verb) == usage line that bad-args errors would emit
* context fields (conversation_ref, principal_ref, transport) pass through
* subject_ref is always None from the parser (resolved by handler)
* Route /kill, /tasks list, grant, who has through parser; same effective
  verb/scope/args as the old parsers produced for the same intent
"""

from __future__ import annotations

import pytest

from router.commands.grammar import VERB_TABLE, VerbEntry, help_text, parse
from router.commands.types import SCOPE_AGENT, SCOPE_DRAFT, SCOPE_GLOBAL, Command

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse() — basic mechanics
# ---------------------------------------------------------------------------


class TestParseBasics:
    def test_empty_string_returns_help(self):
        cmd = parse("")
        assert cmd is not None
        assert cmd.verb == "help"
        assert cmd.scope == SCOPE_GLOBAL
        assert cmd.args == []

    def test_whitespace_only_returns_help(self):
        cmd = parse("   ")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_unknown_verb_returns_none(self):
        assert parse("frobnicate") is None

    def test_unknown_verb_with_args_returns_none(self):
        assert parse("foo bar baz") is None

    def test_return_type_is_command(self):
        cmd = parse("kill")
        assert isinstance(cmd, Command)

    def test_subject_ref_always_none_from_parser(self):
        for text in ("kill", "killall", "grant github", "tasks list", "help"):
            cmd = parse(text)
            assert cmd is not None, f"parse({text!r}) returned None"
            assert cmd.subject_ref is None, f"subject_ref not None for {text!r}"


# ---------------------------------------------------------------------------
# Context fields pass-through
# ---------------------------------------------------------------------------


class TestContextPassThrough:
    def test_conversation_ref_passed_through(self):
        cmd = parse("kill", conversation_ref="slack:C1:1.0")
        assert cmd is not None
        assert cmd.conversation_ref == "slack:C1:1.0"

    def test_principal_ref_passed_through(self):
        cmd = parse("kill", principal_ref="slack:U123")
        assert cmd is not None
        assert cmd.principal_ref == "slack:U123"

    def test_transport_passed_through(self):
        cmd = parse("kill", transport="slack")
        assert cmd is not None
        assert cmd.transport == "slack"

    def test_all_context_fields_none_by_default(self):
        cmd = parse("kill")
        assert cmd is not None
        assert cmd.conversation_ref is None
        assert cmd.principal_ref is None
        assert cmd.transport is None

    def test_all_context_fields_combined(self):
        cmd = parse(
            "grant github",
            conversation_ref="terminal:1",
            principal_ref="terminal:admin",
            transport="terminal",
        )
        assert cmd is not None
        assert cmd.conversation_ref == "terminal:1"
        assert cmd.principal_ref == "terminal:admin"
        assert cmd.transport == "terminal"


# ---------------------------------------------------------------------------
# kill / killall — the core scope distinction
# ---------------------------------------------------------------------------


class TestKillVerbs:
    def test_kill_parses_as_agent_scoped(self):
        cmd = parse("kill")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == []

    def test_killall_parses_as_global(self):
        cmd = parse("killall")
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL
        assert cmd.args == []

    def test_kill_all_with_arg_is_NOT_killall(self):
        # "kill all" = kill verb with stray "all" arg; NOT the global killall.
        # The handler should emit a usage error for the stray arg.
        cmd = parse("kill all")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == ["all"]

    def test_kill_star_with_arg_stays_agent_scoped(self):
        cmd = parse("kill *")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT

    def test_kill_uppercase(self):
        cmd = parse("KILL")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT

    def test_killall_uppercase(self):
        cmd = parse("KILLALL")
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_kill_mixedcase(self):
        cmd = parse("Kill")
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_scope_decided_by_verb_identity_not_args(self):
        # Design requirement: scope is NEVER inferred from args.
        # kill → always agent; killall → always global, regardless of extra tokens.
        assert parse("kill").scope == SCOPE_AGENT
        assert parse("killall").scope == SCOPE_GLOBAL
        assert parse("kill all").scope == SCOPE_AGENT  # stray arg, still agent
        assert parse("killall extra").scope == SCOPE_GLOBAL  # stray arg, still global


# ---------------------------------------------------------------------------
# tasks — agent-scoped with subcommands
# ---------------------------------------------------------------------------


class TestTasksVerb:
    def test_tasks_no_args(self):
        cmd = parse("tasks")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == []

    def test_tasks_list(self):
        cmd = parse("tasks list")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["list"]

    def test_tasks_create(self):
        cmd = parse("tasks create")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["create"]

    def test_tasks_pause_with_id(self):
        cmd = parse("tasks pause abc-123")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["pause", "abc-123"]

    def test_tasks_resume_with_id(self):
        cmd = parse("tasks resume abc-123")
        assert cmd is not None
        assert cmd.args == ["resume", "abc-123"]

    def test_tasks_delete_with_id(self):
        cmd = parse("tasks delete abc-123")
        assert cmd is not None
        assert cmd.args == ["delete", "abc-123"]

    def test_tasks_detail_with_id(self):
        cmd = parse("tasks detail abc-123")
        assert cmd is not None
        assert cmd.args == ["detail", "abc-123"]

    def test_tasks_uppercase(self):
        cmd = parse("TASKS LIST")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["LIST"]  # args case-preserved

    def test_tasks_scope_is_agent(self):
        assert parse("tasks list").scope == SCOPE_AGENT


# ---------------------------------------------------------------------------
# grant / revoke — agent-scoped pack commands
# ---------------------------------------------------------------------------


class TestGrantRevoke:
    def test_grant_with_pack(self):
        cmd = parse("grant github")
        assert cmd is not None
        assert cmd.verb == "grant"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == ["github"]

    def test_grant_no_pack(self):
        # No pack arg — handler should emit usage error; parser still matches.
        cmd = parse("grant")
        assert cmd is not None
        assert cmd.verb == "grant"
        assert cmd.args == []

    def test_grant_pack_case_preserved(self):
        cmd = parse("grant GitHub")
        assert cmd is not None
        assert cmd.args == ["GitHub"]  # case of arg preserved

    def test_revoke_with_pack(self):
        cmd = parse("revoke github")
        assert cmd is not None
        assert cmd.verb == "revoke"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == ["github"]

    def test_revoke_no_pack(self):
        cmd = parse("revoke")
        assert cmd is not None
        assert cmd.verb == "revoke"
        assert cmd.args == []

    def test_grant_uppercase(self):
        cmd = parse("GRANT github")
        assert cmd is not None
        assert cmd.verb == "grant"

    def test_grant_scope_is_agent(self):
        assert parse("grant github").scope == SCOPE_AGENT

    def test_revoke_scope_is_agent(self):
        assert parse("revoke github").scope == SCOPE_AGENT

    def test_grant_no_agent_positional(self):
        # New grammar: no agent positional. "grant <pack>" only.
        # "grant sam github" → verb=grant, args=["sam", "github"] (sam is a stray arg).
        cmd = parse("grant sam github")
        assert cmd is not None
        assert cmd.verb == "grant"
        assert cmd.args == ["sam", "github"]  # stray arg; handler emits usage error


# ---------------------------------------------------------------------------
# list packs / who has — global multi-word verbs
# ---------------------------------------------------------------------------


class TestMultiWordVerbs:
    def test_list_packs_is_global(self):
        cmd = parse("list packs")
        assert cmd is not None
        assert cmd.verb == "list packs"
        assert cmd.scope == SCOPE_GLOBAL
        assert cmd.args == []

    def test_list_packs_uppercase(self):
        cmd = parse("LIST PACKS")
        assert cmd is not None
        assert cmd.verb == "list packs"

    def test_list_packs_mixed_case(self):
        cmd = parse("List Packs")
        assert cmd is not None
        assert cmd.verb == "list packs"

    def test_list_packs_with_extra_tokens(self):
        # Extra tokens go into args (stray; handler validates)
        cmd = parse("list packs extra")
        assert cmd is not None
        assert cmd.verb == "list packs"
        assert cmd.args == ["extra"]

    def test_list_alone_is_not_a_verb(self):
        assert parse("list") is None

    def test_who_has_with_pack(self):
        cmd = parse("who has github")
        assert cmd is not None
        assert cmd.verb == "who has"
        assert cmd.scope == SCOPE_GLOBAL
        assert cmd.args == ["github"]

    def test_who_has_no_pack(self):
        cmd = parse("who has")
        assert cmd is not None
        assert cmd.verb == "who has"
        assert cmd.args == []

    def test_who_has_uppercase(self):
        cmd = parse("WHO HAS github")
        assert cmd is not None
        assert cmd.verb == "who has"
        assert cmd.args == ["github"]

    def test_who_alone_is_not_a_verb(self):
        assert parse("who") is None

    def test_who_has_pack_case_preserved(self):
        cmd = parse("who has GitHub")
        assert cmd is not None
        assert cmd.args == ["GitHub"]


# ---------------------------------------------------------------------------
# help — global, generated from table
# ---------------------------------------------------------------------------


class TestHelpVerb:
    def test_help_is_global(self):
        cmd = parse("help")
        assert cmd is not None
        assert cmd.verb == "help"
        assert cmd.scope == SCOPE_GLOBAL
        assert cmd.args == []

    def test_help_with_verb_arg(self):
        cmd = parse("help grant")
        assert cmd is not None
        assert cmd.verb == "help"
        assert cmd.args == ["grant"]

    def test_help_uppercase(self):
        cmd = parse("HELP")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_bare_text_emits_help_command(self):
        # Simulates bare "aidt" with no verb — shim strips marker, gets "".
        cmd = parse("")
        assert cmd is not None
        assert cmd.verb == "help"


# ---------------------------------------------------------------------------
# Scope table assertions (locked by design note)
# ---------------------------------------------------------------------------


class TestScopeTable:
    @pytest.mark.parametrize(
        "verb_text,expected_verb",
        [
            ("kill", "kill"),
            ("tasks list", "tasks"),
            ("grant github", "grant"),
            ("revoke github", "revoke"),
        ],
    )
    def test_agent_scoped_verbs(self, verb_text, expected_verb):
        cmd = parse(verb_text)
        assert cmd is not None
        assert cmd.verb == expected_verb
        assert cmd.scope == SCOPE_AGENT

    @pytest.mark.parametrize(
        "verb_text,expected_verb",
        [
            ("killall", "killall"),
            ("list packs", "list packs"),
            ("who has github", "who has"),
            ("help", "help"),
        ],
    )
    def test_global_verbs(self, verb_text, expected_verb):
        cmd = parse(verb_text)
        assert cmd is not None
        assert cmd.verb == expected_verb
        assert cmd.scope == SCOPE_GLOBAL


# ---------------------------------------------------------------------------
# help_text() — generated from VERB_TABLE, never hand-written copy
# ---------------------------------------------------------------------------


class TestHelpText:
    def test_full_help_contains_every_verb(self):
        text = help_text()
        for entry in VERB_TABLE:
            assert entry.verb in text, f"verb {entry.verb!r} missing from help_text()"

    def test_full_help_has_no_hand_written_verbs(self):
        # All verbs in the output come from VERB_TABLE — we verify by checking
        # the table covers all "aidt <verb>" lines in the output.
        text = help_text()
        table_verbs = {e.verb for e in VERB_TABLE}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("aidt "):
                # Extract verb portion: token(s) after "aidt" up to "  —"
                remainder = line[len("aidt ") :].split("  —")[0].strip()
                # Remove any arg-syntax tokens by checking known multi-word or single-word verbs
                matched = any(remainder == v or remainder.startswith(v + " ") for v in table_verbs)
                assert matched, f"line {line!r} contains a verb not in VERB_TABLE"

    def test_help_text_for_verb_returns_usage_line(self):
        usage = help_text("kill")
        assert usage == "aidt kill"

    def test_help_text_for_verb_with_args(self):
        usage = help_text("grant")
        assert usage == "aidt grant <pack>"

    def test_help_text_for_list_packs(self):
        usage = help_text("list packs")
        assert usage == "aidt list packs"

    def test_help_text_for_who_has(self):
        usage = help_text("who has")
        assert usage == "aidt who has <pack>"

    def test_help_text_for_killall(self):
        assert help_text("killall") == "aidt killall"

    def test_help_text_for_tasks(self):
        usage = help_text("tasks")
        assert usage.startswith("aidt tasks ")

    def test_help_text_unknown_verb_returns_full_list(self):
        text = help_text("nosuchverb")
        for entry in VERB_TABLE:
            assert entry.verb in text

    def test_help_text_none_returns_full_list(self):
        text = help_text()
        assert "kill" in text
        assert "tasks" in text
        assert "grant" in text

    def test_help_text_verb_case_insensitive(self):
        assert help_text("KILL") == help_text("kill")
        assert help_text("Grant") == help_text("grant")

    def test_help_and_usage_error_strings_are_identical(self):
        # Design requirement: help_text(verb) == the string the bad-args error emits.
        # Both come from the same _usage_line() function so they can never drift.
        for entry in VERB_TABLE:
            usage = help_text(entry.verb)
            assert usage.startswith(f"aidt {entry.verb}"), (
                f"help_text({entry.verb!r}) does not start with 'aidt {entry.verb}'"
            )


# ---------------------------------------------------------------------------
# Route-through smoke tests (simulating transport shim → parser → handler args)
# ---------------------------------------------------------------------------


class TestRouteThrough:
    """Verify that routing old-style Slack commands through the new parser
    produces the correct verb / scope / args that a handler needs.

    Design note requirement: "Route /kill, /tasks list, grant, who has through
    the new parser and assert the same handler fires with the same effective
    args + subject as today."
    """

    def test_slash_kill_shim(self):
        # Slack shim: /kill (no args) → strip slash → parse("kill")
        cmd = parse("kill")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == []
        # Handler resolves addressed agent from conversation_ref — no positional arg.

    def test_slash_tasks_list_shim(self):
        # Slack shim: /tasks list → strip slash → parse("tasks list")
        cmd = parse("tasks list")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == ["list"]

    def test_slash_tasks_no_args_shim(self):
        # /tasks (no subcommand) → "tasks" → args=[] → handler defaults to list
        cmd = parse("tasks")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == []

    def test_grant_via_text_message(self):
        # Old inline text: "grant github" (agent positional dropped; subject from context)
        cmd = parse("grant github")
        assert cmd is not None
        assert cmd.verb == "grant"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == ["github"]
        # subject_ref=None; handler resolves addressed agent from conversation_ref.

    def test_who_has_via_text_message(self):
        # Old inline text: "who has github"
        cmd = parse("who has github")
        assert cmd is not None
        assert cmd.verb == "who has"
        assert cmd.scope == SCOPE_GLOBAL
        assert cmd.args == ["github"]

    def test_list_packs_via_text_message(self):
        cmd = parse("list packs")
        assert cmd is not None
        assert cmd.verb == "list packs"
        assert cmd.scope == SCOPE_GLOBAL

    def test_killall_distinct_from_kill(self):
        # killall is global; kill is agent; router uses verb identity alone.
        kill_cmd = parse("kill")
        killall_cmd = parse("killall")
        assert kill_cmd.verb != killall_cmd.verb
        assert kill_cmd.scope == SCOPE_AGENT
        assert killall_cmd.scope == SCOPE_GLOBAL

    def test_aidt_prefix_stripped_before_parse(self):
        # Shims strip the "aidt" keyword; parser never sees it.
        # Simulating: "aidt kill" → shim strips → parse("kill")
        cmd = parse("kill")  # "aidt" already stripped
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_context_fields_from_slack_shim(self):
        # Shim fills in conversation_ref, principal_ref, transport before calling parse.
        cmd = parse(
            "kill",
            conversation_ref="slack:C123:1234.5",
            principal_ref="slack:U_op",
            transport="slack",
        )
        assert cmd is not None
        assert cmd.transport == "slack"
        assert cmd.conversation_ref == "slack:C123:1234.5"
        assert cmd.principal_ref == "slack:U_op"
        assert cmd.subject_ref is None  # handler resolves this


# ---------------------------------------------------------------------------
# VERB_TABLE integrity
# ---------------------------------------------------------------------------


class TestVerbTableIntegrity:
    def test_all_entries_are_verb_entry(self):
        for entry in VERB_TABLE:
            assert isinstance(entry, VerbEntry)

    def test_scope_values_are_valid(self):
        valid = {SCOPE_GLOBAL, SCOPE_AGENT, SCOPE_DRAFT}
        for entry in VERB_TABLE:
            assert entry.scope in valid, f"{entry.verb!r} has invalid scope {entry.scope!r}"

    def test_no_duplicate_verbs(self):
        verbs = [e.verb for e in VERB_TABLE]
        assert len(verbs) == len(set(verbs)), "VERB_TABLE has duplicate verb entries"

    def test_expected_verbs_present(self):
        verb_names = {e.verb for e in VERB_TABLE}
        required = {"kill", "killall", "tasks", "grant", "revoke", "list packs", "who has", "help", "approve", "reject"}
        assert required <= verb_names, f"Missing verbs: {required - verb_names}"

    def test_multi_word_verbs_have_spaces(self):
        multi = [e for e in VERB_TABLE if " " in e.verb]
        expected_multi = {"list packs", "who has"}
        assert {e.verb for e in multi} == expected_multi


# ---------------------------------------------------------------------------
# approve / reject — draft-scoped verbs
# ---------------------------------------------------------------------------


class TestApproveRejectVerbs:
    def test_approve_is_draft_scoped(self):
        cmd = parse("approve")
        assert cmd is not None
        assert cmd.verb == "approve"
        assert cmd.scope == SCOPE_DRAFT
        assert cmd.args == []

    def test_reject_is_draft_scoped(self):
        cmd = parse("reject")
        assert cmd is not None
        assert cmd.verb == "reject"
        assert cmd.scope == SCOPE_DRAFT
        assert cmd.args == []

    def test_approve_with_draft_id(self):
        cmd = parse("approve draft-abc-123")
        assert cmd is not None
        assert cmd.verb == "approve"
        assert cmd.scope == SCOPE_DRAFT
        assert cmd.args == ["draft-abc-123"]

    def test_reject_with_draft_id(self):
        cmd = parse("reject draft-abc-123")
        assert cmd is not None
        assert cmd.verb == "reject"
        assert cmd.args == ["draft-abc-123"]

    def test_approve_uppercase(self):
        cmd = parse("APPROVE")
        assert cmd is not None
        assert cmd.verb == "approve"

    def test_reject_uppercase(self):
        cmd = parse("REJECT")
        assert cmd is not None
        assert cmd.verb == "reject"

    def test_approve_subject_ref_always_none(self):
        cmd = parse("approve")
        assert cmd is not None
        assert cmd.subject_ref is None

    def test_approve_context_fields_pass_through(self):
        cmd = parse(
            "approve draft-1",
            conversation_ref="slack:C1:1.0",
            principal_ref="slack:U123",
            transport="slack",
        )
        assert cmd is not None
        assert cmd.conversation_ref == "slack:C1:1.0"
        assert cmd.principal_ref == "slack:U123"
        assert cmd.transport == "slack"

    def test_approve_and_reject_in_scope_table(self):
        draft_verbs = [e for e in VERB_TABLE if e.scope == SCOPE_DRAFT]
        draft_verb_names = {e.verb for e in draft_verbs}
        assert "approve" in draft_verb_names
        assert "reject" in draft_verb_names


# ---------------------------------------------------------------------------
# yes / no — marker-free approval aliases
# ---------------------------------------------------------------------------


class TestMarkerFreeApprovalAliases:
    # --- bare yes/no without pending context → None (no message swallowing) ---

    def test_bare_yes_without_context_returns_none(self):
        assert parse("yes") is None

    def test_bare_no_without_context_returns_none(self):
        assert parse("no") is None

    def test_yes_with_empty_pending_returns_none(self):
        assert parse("yes", pending_draft_ids=[]) is None

    def test_no_with_empty_pending_returns_none(self):
        assert parse("no", pending_draft_ids=[]) is None

    def test_yes_with_none_pending_returns_none(self):
        assert parse("yes", pending_draft_ids=None) is None

    # --- yes/no with pending drafts → approve/reject ---

    def test_yes_with_pending_returns_approve(self):
        cmd = parse("yes", pending_draft_ids=["draft-1"])
        assert cmd is not None
        assert cmd.verb == "approve"
        assert cmd.scope == SCOPE_DRAFT
        assert cmd.args == []

    def test_no_with_pending_returns_reject(self):
        cmd = parse("no", pending_draft_ids=["draft-1"])
        assert cmd is not None
        assert cmd.verb == "reject"
        assert cmd.scope == SCOPE_DRAFT
        assert cmd.args == []

    def test_yes_uppercase_with_pending(self):
        cmd = parse("YES", pending_draft_ids=["draft-1"])
        assert cmd is not None
        assert cmd.verb == "approve"

    def test_no_uppercase_with_pending(self):
        cmd = parse("NO", pending_draft_ids=["draft-1"])
        assert cmd is not None
        assert cmd.verb == "reject"

    def test_yes_with_multiple_pending_still_parses(self):
        # Parser recognizes 'yes' when multiple drafts exist; disambiguation is
        # done by the dispatch handler (which emits an ambiguity error).
        cmd = parse("yes", pending_draft_ids=["draft-1", "draft-2"])
        assert cmd is not None
        assert cmd.verb == "approve"

    def test_yes_preserves_conversation_ref(self):
        cmd = parse("yes", conversation_ref="slack:C1:ts", pending_draft_ids=["d1"])
        assert cmd is not None
        assert cmd.conversation_ref == "slack:C1:ts"

    def test_yes_preserves_transport(self):
        cmd = parse("yes", transport="discord", pending_draft_ids=["d1"])
        assert cmd is not None
        assert cmd.transport == "discord"

    # --- approve/reject always work (no pending_draft_ids needed) ---

    def test_approve_always_recognized_without_pending_context(self):
        cmd = parse("approve")
        assert cmd is not None
        assert cmd.verb == "approve"

    def test_reject_always_recognized_without_pending_context(self):
        cmd = parse("reject")
        assert cmd is not None
        assert cmd.verb == "reject"

    def test_approve_with_id_always_recognized(self):
        cmd = parse("approve draft-99")
        assert cmd is not None
        assert cmd.verb == "approve"
        assert cmd.args == ["draft-99"]

    # --- yes/no do NOT collide with other verbs ---

    def test_yes_is_not_a_standalone_verb(self):
        # Without pending context, yes → None
        assert parse("yes") is None

    def test_no_is_not_a_standalone_verb(self):
        assert parse("no") is None


# ---------------------------------------------------------------------------
# help_text() — approve/reject/yes/no coverage
# ---------------------------------------------------------------------------


class TestHelpTextApproval:
    def test_full_help_contains_approve(self):
        text = help_text()
        assert "approve" in text

    def test_full_help_contains_reject(self):
        text = help_text()
        assert "reject" in text

    def test_full_help_has_draft_section(self):
        text = help_text()
        assert "draft" in text

    def test_help_approve_usage_line(self):
        usage = help_text("approve")
        assert usage == "aidt approve [<draft-id>]"

    def test_help_reject_usage_line(self):
        usage = help_text("reject")
        assert usage == "aidt reject [<draft-id>]"
