"""Tests for router.mentions — @mention parsing and target resolution."""

from __future__ import annotations

import pytest

from router.mentions import last_mentioned, parse_mentions, resolve_target_agent

pytestmark = pytest.mark.unit


AGENTS = ["lisa", "sam", "dave", "maya"]


class TestParseMentions:
    def test_empty_text(self):
        assert parse_mentions("", AGENTS) == []

    def test_no_known_agents(self):
        assert parse_mentions("hello @lisa", []) == []

    def test_plain_name_mention(self):
        assert parse_mentions("hey @lisa can you help?", AGENTS) == ["lisa"]

    def test_plain_name_case_insensitive(self):
        assert parse_mentions("hey @Lisa", AGENTS) == ["lisa"]
        assert parse_mentions("hey @SAM!", AGENTS) == ["sam"]

    def test_unknown_name_ignored(self):
        assert parse_mentions("hey @alex", AGENTS) == []

    def test_multiple_mentions_in_order(self):
        result = parse_mentions("first @sam then @maya then @lisa", AGENTS)
        assert result == ["sam", "maya", "lisa"]

    def test_slack_user_id_mention(self):
        bot_user_map = {"U_BOT_LISA": "lisa"}
        result = parse_mentions("hi <@U_BOT_LISA>", AGENTS, bot_user_map)
        assert result == ["lisa"]

    def test_slack_user_id_with_label(self):
        """Slack user mentions can include a label, e.g. <@U123|lisa>."""
        bot_user_map = {"U_BOT_SAM": "sam"}
        result = parse_mentions("hi <@U_BOT_SAM|sam> please help", AGENTS, bot_user_map)
        assert result == ["sam"]

    def test_mixed_mentions_ordered_by_position(self):
        bot_user_map = {"U_BOT_LISA": "lisa"}
        result = parse_mentions("@sam then <@U_BOT_LISA>", AGENTS, bot_user_map)
        assert result == ["sam", "lisa"]

    def test_email_not_treated_as_mention(self):
        """``foo@bar.com`` should not produce a spurious mention."""
        assert parse_mentions("send to user@sam.example.com", AGENTS) == []

    def test_agent_prefix_boundary(self):
        """``@samuel`` must not match ``sam``."""
        assert parse_mentions("hi @samuel", AGENTS) == []

    def test_punctuation_after_mention(self):
        assert parse_mentions("hello, @lisa! how are you?", AGENTS) == ["lisa"]


class TestLastMentioned:
    def test_last_wins(self):
        assert last_mentioned("@sam hello @maya", AGENTS) == "maya"

    def test_no_mentions_returns_none(self):
        assert last_mentioned("no mentions here", AGENTS) is None


class TestResolveTargetAgent:
    def test_mention_wins_over_active(self):
        agent, mentioned = resolve_target_agent("@maya please help", AGENTS, active_agent="lisa")
        assert agent == "maya"
        assert mentioned is True

    def test_last_mention_wins(self):
        agent, mentioned = resolve_target_agent("@sam and @maya", AGENTS, active_agent="lisa")
        assert agent == "maya"
        assert mentioned is True

    def test_falls_back_to_active_agent(self):
        agent, mentioned = resolve_target_agent("just a reply", AGENTS, active_agent="sam")
        assert agent == "sam"
        assert mentioned is False

    def test_falls_back_to_default_when_no_active(self):
        agent, mentioned = resolve_target_agent("just a reply", AGENTS, default_agent="lisa")
        assert agent == "lisa"
        assert mentioned is False

    def test_returns_none_when_nothing_matches(self):
        agent, mentioned = resolve_target_agent("hi", AGENTS)
        assert agent is None
        assert mentioned is False

    def test_unknown_active_agent_ignored(self):
        agent, mentioned = resolve_target_agent("hi", AGENTS, active_agent="stranger", default_agent="lisa")
        assert agent == "lisa"
        assert mentioned is False

    def test_bot_user_id_mention_wins(self):
        agent, mentioned = resolve_target_agent(
            "<@U_BOT_SAM> please",
            AGENTS,
            bot_user_map={"U_BOT_SAM": "sam"},
            active_agent="lisa",
            default_agent="lisa",
        )
        assert agent == "sam"
        assert mentioned is True

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_or_none_text(self, text):
        agent, mentioned = resolve_target_agent(text or "", AGENTS, default_agent="lisa")
        assert agent == "lisa"
        assert mentioned is False
