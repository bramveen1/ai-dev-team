"""Classify dispatch failures into user-facing error categories.

Each failure path in the router generates a short correlation id (8 hex chars)
that is logged at ERROR level and included in the Slack message so users can
quote it back when asking for a log dive.
"""

from __future__ import annotations

import secrets


def make_correlation_id() -> str:
    """Return an 8-character lowercase hex string for correlating log lines."""
    return secrets.token_hex(4)


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map an exception to ``(category, user_message_template)``.

    The returned message template may contain ``{corr_id}`` which callers
    should format before posting to Slack.

    Categories (in match order):
    - ``ApiOverload``     — 429/529 from Anthropic; ask user to retry.
    - ``ApiAuthQuota``    — other 4xx from Anthropic; escalate to operator.
    - ``Timeout``         — wall-clock dispatch timeout.
    - ``CliError``        — non-zero CLI exit with no recognised API code.
    - ``RouterException`` — any other exception (internal bug).
    """
    # Import here to avoid a circular import; this module is intentionally
    # lightweight and imported by app.py which already imports dispatcher.py.
    from router.dispatcher import ApiError, DispatchError, DispatchTimeoutError

    if isinstance(exc, ApiError):
        if exc.status_code in (429, 529):
            return (
                "ApiOverload",
                "Anthropic is overloaded — please retry in a minute. (ref: `{corr_id}`)",
            )
        return (
            "ApiAuthQuota",
            "Hit an API limit — flagging to Bram. (ref: `{corr_id}`)",
        )

    if isinstance(exc, DispatchTimeoutError):
        return (
            "Timeout",
            "Ran out of time on that one — want me to retry with a bigger budget? (ref: `{corr_id}`)",
        )

    if isinstance(exc, DispatchError):
        return (
            "CliError",
            "Worker exited with an error. Logs: `{corr_id}`.",
        )

    return (
        "RouterException",
        "Internal error — logs: `{corr_id}`. Bram will see this.",
    )


def build_error_message(exc: Exception, corr_id: str) -> tuple[str, str]:
    """Return ``(category, slack_message)`` with the correlation id substituted."""
    category, template = classify_error(exc)
    return category, template.format(corr_id=corr_id)
