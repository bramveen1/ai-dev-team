"""Convert Markdown formatting to Slack mrkdwn syntax.

Slack uses its own markup dialect (mrkdwn) which differs from standard
Markdown. This module converts the most common Markdown patterns so
agent responses render correctly in Slack.

Reference: https://api.slack.com/reference/surfaces/formatting
"""

from __future__ import annotations

import re


def md_to_slack(text: str) -> str:
    """Convert Markdown-formatted text to Slack mrkdwn.

    Handles: bold, italic, strikethrough, links, and headings.
    Preserves code blocks and inline code (which are the same in both formats).
    """
    if not text:
        return text

    # Split on code blocks to avoid mangling code content
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)

    converted = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Inside a code block or inline code — leave as-is
            converted.append(part)
        else:
            converted.append(_convert_segment(part))

    return "".join(converted)


def _convert_segment(text: str) -> str:
    """Convert Markdown formatting in a non-code segment."""
    # Italic *text* FIRST — convert standalone single-asterisk italic to _text_
    # before we create new single-asterisk bold from **.
    # Match *text* that isn't preceded/followed by another *
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)

    # Bold: **text** or __text__ → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Headings: # text → *text*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    return text
