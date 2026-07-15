"""Convert Markdown formatting to Slack mrkdwn syntax.

Slack uses its own markup dialect (mrkdwn) which differs from standard
Markdown. This module converts the most common Markdown patterns so
agent responses render correctly in Slack.

Reference: https://api.slack.com/reference/surfaces/formatting
"""

from __future__ import annotations

import re


def md_to_slack(text: str, mention_user_ids: dict[str, str] | None = None) -> str:
    """Convert Markdown-formatted text to Slack mrkdwn.

    Handles: bold, italic, strikethrough, links, headings, and unordered
    lists. Preserves code blocks and inline code (which are the same in both
    formats).

    When ``mention_user_ids`` (lower-case name → Slack user ID) is given,
    plain-text @mentions of those names are rewritten to real ``<@UID>``
    mentions — this is how agent-written "@lisa" / "@Dev Lisa" become
    clickable mentions that notify (and, for persona bots, dispatch) the
    target. Unknown names stay as plain text.
    """
    if not text:
        return text

    mention_re = _build_mention_re(mention_user_ids) if mention_user_ids else None

    # Split on code blocks to avoid mangling code content
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)

    converted = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Inside a code block or inline code — leave as-is
            converted.append(part)
        else:
            segment = _convert_segment(part)
            if mention_re is not None:
                segment = mention_re.sub(
                    lambda m: f"<@{mention_user_ids[m.group(1).lower()]}>",
                    segment,
                )
            converted.append(segment)

    return "".join(converted)


def _build_mention_re(mention_user_ids: dict[str, str]) -> re.Pattern | None:
    """Compile a pattern matching ``@<known name>`` (multi-word names allowed).

    Longest names are tried first so "@Dev Lisa" wins over a hypothetical
    "@Dev". The leading boundary mirrors the inbound mention parser: the ``@``
    must open the string or follow a non-word char, so ``email@example.com``
    never matches.
    """
    names = sorted((n for n in mention_user_ids if n), key=len, reverse=True)
    if not names:
        return None
    alternation = "|".join(re.escape(name) for name in names)
    return re.compile(r"(?:^|(?<=\W))@(" + alternation + r")\b", re.IGNORECASE)


def _convert_segment(text: str) -> str:
    """Convert Markdown formatting in a non-code segment."""
    # Unordered list items FIRST: "- item" / "* item" → "• item". Slack has no
    # list markup, so dashes render literally without this. Indentation is kept
    # so nested bullets stay nested. Requiring whitespace after the marker
    # keeps horizontal rules (---) and bold/italic at line start (*text*)
    # untouched, and running before the italic rule keeps "* item" markers out
    # of its reach.
    text = re.sub(r"^([ \t]*)[-*][ \t]+", r"\1• ", text, flags=re.MULTILINE)

    # Italic *text* — convert standalone single-asterisk italic to _text_
    # before we create new single-asterisk bold from **.
    # Require asterisks to hug non-space, non-asterisk content so that
    # arithmetic ('5 * 3') and glob patterns with surrounding spaces are not
    # mistaken for italic markers.
    text = re.sub(r"(?<!\*)\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?!\*)", r"_\1_", text)

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
