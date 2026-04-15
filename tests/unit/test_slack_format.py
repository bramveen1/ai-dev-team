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

    def test_list_items_preserved(self):
        text = "- Item one\n- Item two"
        assert md_to_slack(text) == "- Item one\n- Item two"
