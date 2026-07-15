"""Unit tests for router.slack_format — Markdown → Slack mrkdwn conversion."""

import pytest

from router.slack_format import md_to_slack


@pytest.mark.unit
class TestMdToSlack:
    """Tests for md_to_slack()."""

    def test_empty_string(self):
        assert md_to_slack("") == ""

    def test_none_passthrough(self):
        assert md_to_slack(None) is None

    def test_plain_text_unchanged(self):
        assert md_to_slack("Hello world") == "Hello world"

    # --- Bold ---

    def test_double_asterisk_bold(self):
        assert md_to_slack("**bold**") == "*bold*"

    def test_double_underscore_bold(self):
        assert md_to_slack("__bold__") == "*bold*"

    def test_bold_in_sentence(self):
        result = md_to_slack("The **temperature** is 17°C")
        assert result == "The *temperature* is 17°C"

    def test_multiple_bold(self):
        result = md_to_slack("**Conditions:** Cloudy, **Wind:** Light")
        assert result == "*Conditions:* Cloudy, *Wind:* Light"

    # --- Italic ---

    def test_single_asterisk_italic(self):
        assert md_to_slack("*italic*") == "_italic_"

    def test_italic_in_sentence(self):
        result = md_to_slack("This is *important* stuff")
        assert result == "This is _important_ stuff"

    # --- Strikethrough ---

    def test_strikethrough(self):
        assert md_to_slack("~~removed~~") == "~removed~"

    # --- Links ---

    def test_markdown_link(self):
        result = md_to_slack("[click here](https://example.com)")
        assert result == "<https://example.com|click here>"

    # --- Headings ---

    def test_h1_heading(self):
        assert md_to_slack("# Heading") == "*Heading*"

    def test_h3_heading(self):
        assert md_to_slack("### Sub-heading") == "*Sub-heading*"

    def test_heading_in_multiline(self):
        text = "Intro\n## Details\nBody text"
        result = md_to_slack(text)
        assert result == "Intro\n*Details*\nBody text"

    # --- Code preservation ---

    def test_inline_code_preserved(self):
        result = md_to_slack("Run `**not bold**` to test")
        assert result == "Run `**not bold**` to test"

    def test_code_block_preserved(self):
        text = "Before\n```\n**bold** in code\n```\nAfter **real bold**"
        result = md_to_slack(text)
        assert "```\n**bold** in code\n```" in result
        assert "*real bold*" in result

    # --- Combined / real-world ---

    def test_weather_message(self):
        """Reproduce the exact issue from the screenshot."""
        msg = (
            "Here's the forecast for **Maastricht on Friday evening** (April 17):\n\n"
            "- **Conditions:** Cloudy, but dry\n"
            "- **Temperature:** 17°C\n"
            "- **Wind:** Light, 4-6 km/h\n"
        )
        result = md_to_slack(msg)
        assert "**" not in result
        assert "*Maastricht on Friday evening*" in result
        assert "*Conditions:*" in result
        assert "*Temperature:*" in result

    # --- Unordered lists ---

    def test_dash_list_items_become_bullets(self):
        text = "- Item one\n- Item two"
        assert md_to_slack(text) == "• Item one\n• Item two"

    def test_asterisk_list_items_become_bullets(self):
        text = "* Item one\n* Item two"
        assert md_to_slack(text) == "• Item one\n• Item two"

    def test_nested_list_indentation_preserved(self):
        text = "- Parent\n  - Child\n    - Grandchild"
        assert md_to_slack(text) == "• Parent\n  • Child\n    • Grandchild"

    def test_list_item_with_bold(self):
        assert md_to_slack("- **Conditions:** Cloudy") == "• *Conditions:* Cloudy"

    def test_ordered_list_unchanged(self):
        text = "1. First\n2. Second"
        assert md_to_slack(text) == "1. First\n2. Second"

    def test_horizontal_rule_not_converted(self):
        assert md_to_slack("---") == "---"

    def test_mid_line_dash_not_converted(self):
        assert md_to_slack("well - that works") == "well - that works"

    def test_line_starting_with_italic_not_converted_to_bullet(self):
        assert md_to_slack("*emphasis* at line start") == "_emphasis_ at line start"

    def test_list_markers_in_code_block_preserved(self):
        text = "```\n- not a bullet\n```"
        assert md_to_slack(text) == "```\n- not a bullet\n```"

    # --- Outbound @mention linkification ---

    def test_known_mention_rewritten_to_user_id(self):
        assert md_to_slack("ping @lisa please", {"lisa": "U_LISA"}) == "ping <@U_LISA> please"

    def test_mention_matching_is_case_insensitive(self):
        assert md_to_slack("ping @Lisa", {"lisa": "U_LISA"}) == "ping <@U_LISA>"

    def test_multi_word_display_name_wins_over_prefix(self):
        ids = {"dev lisa": "U_LISA", "dev": "U_DEV"}
        assert md_to_slack("cc @Dev Lisa", ids) == "cc <@U_LISA>"

    def test_unknown_mention_left_as_plain_text(self):
        assert md_to_slack("hey @stranger", {"lisa": "U_LISA"}) == "hey @stranger"

    def test_email_address_not_rewritten(self):
        assert md_to_slack("mail lisa@example.com", {"lisa": "U_LISA"}) == "mail lisa@example.com"

    def test_mention_in_code_not_rewritten(self):
        assert md_to_slack("run `@lisa` and ```\n@lisa\n```", {"lisa": "U_LISA"}) == "run `@lisa` and ```\n@lisa\n```"

    def test_mention_inside_list_item(self):
        assert md_to_slack("- ask @lisa", {"lisa": "U_LISA"}) == "• ask <@U_LISA>"

    def test_no_mention_map_leaves_text_unchanged(self):
        assert md_to_slack("ping @lisa", None) == "ping @lisa"
        assert md_to_slack("ping @lisa", {}) == "ping @lisa"

    # --- Standalone / arithmetic asterisks (issue #461) ---

    def test_arithmetic_asterisks_not_converted(self):
        """Standalone asterisks used as multiplication must not be italicised."""
        result = md_to_slack("rate is 5 * 3 = 15 and 2 * 4 = 8")
        assert result == "rate is 5 * 3 = 15 and 2 * 4 = 8"

    def test_single_standalone_asterisk_preserved(self):
        assert md_to_slack("a * b") == "a * b"

    def test_multiple_standalone_asterisks_preserved(self):
        result = md_to_slack("x * y * z")
        assert result == "x * y * z"

    def test_italic_still_works_after_fix(self):
        """Italic markers that hug content must still be converted."""
        assert md_to_slack("*italic*") == "_italic_"

    def test_italic_with_arithmetic_in_same_string(self):
        """Italic and arithmetic asterisks can coexist."""
        result = md_to_slack("use *factor* like 2 * 3")
        assert result == "use _factor_ like 2 * 3"
