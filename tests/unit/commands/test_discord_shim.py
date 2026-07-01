"""Tests for router.commands.discord_shim — the Discord-specific entry shim.

Covers:
* @mention + aidt keyword → Command (verb, scope, args, transport)
* bare aidt keyword (no @mention) → Command
* Bare / empty aidt → help command
* Unknown verb after aidt → None (fall-through)
* Non-aidt message → None (fall-through to normal dispatch)
* Case-insensitive aidt matching
* Context fields (conversation_ref, principal_ref, transport) pass through
"""

from __future__ import annotations

import pytest

from router.commands.discord_shim import parse_from_discord_message
from router.commands.types import SCOPE_AGENT, SCOPE_GLOBAL

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# @mention + aidt prefix
# ---------------------------------------------------------------------------


class TestMentionAidtPrefix:
    def test_mention_kill_produces_kill_command(self):
        cmd = parse_from_discord_message("<@123456789012345678> aidt kill")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.transport == "discord"

    def test_mention_killall_produces_killall(self):
        cmd = parse_from_discord_message("<@111222333444555666> aidt killall")
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_mention_tasks_list(self):
        cmd = parse_from_discord_message("<@111> aidt tasks list")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["list"]

    def test_mention_help(self):
        cmd = parse_from_discord_message("<@111> aidt help")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_mention_grant_with_pack(self):
        cmd = parse_from_discord_message("<@111> aidt grant github")
        assert cmd is not None
        assert cmd.verb == "grant"
        assert cmd.args == ["github"]

    def test_mention_list_packs(self):
        cmd = parse_from_discord_message("<@111> aidt list packs")
        assert cmd is not None
        assert cmd.verb == "list packs"
        assert cmd.scope == SCOPE_GLOBAL

    def test_mention_who_has(self):
        cmd = parse_from_discord_message("<@111> aidt who has github")
        assert cmd is not None
        assert cmd.verb == "who has"
        assert cmd.args == ["github"]


# ---------------------------------------------------------------------------
# bare aidt keyword (no @mention — e.g. thread follow-up)
# ---------------------------------------------------------------------------


class TestBareAidtKeyword:
    def test_bare_aidt_kill(self):
        cmd = parse_from_discord_message("aidt kill")
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_bare_aidt_killall(self):
        cmd = parse_from_discord_message("aidt killall")
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_bare_aidt_tasks_list(self):
        cmd = parse_from_discord_message("aidt tasks list")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["list"]

    def test_bare_aidt_case_insensitive(self):
        cmd = parse_from_discord_message("AIDT kill")
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_bare_aidt_mixed_case(self):
        cmd = parse_from_discord_message("Aidt killall")
        assert cmd is not None
        assert cmd.verb == "killall"


# ---------------------------------------------------------------------------
# Bare marker → help (discoverability)
# ---------------------------------------------------------------------------


class TestBareMarker:
    def test_bare_aidt_alone_returns_help(self):
        cmd = parse_from_discord_message("aidt")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_mention_bare_aidt_returns_help(self):
        cmd = parse_from_discord_message("<@123> aidt")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_mention_only_no_aidt_returns_none(self):
        """Bare @mention without aidt keyword → None (normal agent dispatch)."""
        assert parse_from_discord_message("<@123>") is None

    def test_mention_only_with_text_no_aidt_returns_none(self):
        assert parse_from_discord_message("<@123> please review my PR") is None


# ---------------------------------------------------------------------------
# Unknown verb → None
# ---------------------------------------------------------------------------


class TestUnknownVerb:
    def test_unknown_verb_after_aidt_returns_none(self):
        assert parse_from_discord_message("aidt frobnicate") is None

    def test_unknown_verb_after_mention_aidt_returns_none(self):
        assert parse_from_discord_message("<@111> aidt frobnicate") is None


# ---------------------------------------------------------------------------
# Non-aidt messages → None (fall-through)
# ---------------------------------------------------------------------------


class TestNonAidtMessages:
    def test_plain_message_returns_none(self):
        assert parse_from_discord_message("hey can you review this PR?") is None

    def test_message_with_mention_no_aidt_returns_none(self):
        assert parse_from_discord_message("<@111> can you review this PR?") is None

    def test_empty_string_returns_none(self):
        assert parse_from_discord_message("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_from_discord_message("   ") is None


# ---------------------------------------------------------------------------
# Context fields pass-through
# ---------------------------------------------------------------------------


class TestContextPassThrough:
    def test_conversation_ref_passed_through(self):
        cmd = parse_from_discord_message("aidt kill", conversation_ref="discord:1:2:3")
        assert cmd is not None
        assert cmd.conversation_ref == "discord:1:2:3"

    def test_principal_ref_passed_through(self):
        cmd = parse_from_discord_message("aidt kill", principal_ref="discord:123456789")
        assert cmd is not None
        assert cmd.principal_ref == "discord:123456789"

    def test_transport_is_discord(self):
        cmd = parse_from_discord_message("aidt kill")
        assert cmd is not None
        assert cmd.transport == "discord"

    def test_all_context_fields_combined(self):
        cmd = parse_from_discord_message(
            "<@111> aidt killall",
            conversation_ref="discord:1:100:0",
            principal_ref="discord:999",
        )
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.conversation_ref == "discord:1:100:0"
        assert cmd.principal_ref == "discord:999"
        assert cmd.transport == "discord"

    def test_subject_ref_always_none_from_parser(self):
        cmd = parse_from_discord_message("aidt kill")
        assert cmd is not None
        assert cmd.subject_ref is None


# ---------------------------------------------------------------------------
# Discord nickname-mention form (<@!snowflake>)
# ---------------------------------------------------------------------------


class TestNicknameMentionForm:
    def test_nickname_mention_stripped(self):
        """Old Discord ``<@!SNOWFLAKE>`` mention form is also stripped."""
        cmd = parse_from_discord_message("<@!123456789> aidt kill")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.transport == "discord"
