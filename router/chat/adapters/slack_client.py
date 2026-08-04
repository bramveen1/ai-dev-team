"""Slack SDK boundary — the one core-facing module allowed to import ``slack_sdk``.

Part of the #553 migration. The Slack transport SDK is quarantined under
``router/chat/adapters/``; everything outside the adapter package imports the
client type and error helpers *from here* instead of reaching for ``slack_sdk``
directly. That keeps the raw ``import slack_sdk`` grep-clean outside the adapter
package (enforced by ``tests/unit/test_slack_sdk_boundary.py``), so core stays
transport-neutral and a future backend swap touches only this package.

The three previous non-adapter import sites — ``router.runtime`` (the workers
``AsyncWebClient`` factory), ``router.app`` (workers-bot ``auth.test``) and
``router.scheduled_tasks.scheduler`` (archived-thread error classification) —
now consume ``build_web_client`` / ``slack_error_code`` / the re-exported
``AsyncWebClient`` type from this module. Behavior is unchanged.
"""

from __future__ import annotations

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "AsyncWebClient",
    "SlackApiError",
    "build_web_client",
    "slack_error_code",
]


def build_web_client(token: str) -> AsyncWebClient:
    """Construct a Slack ``AsyncWebClient`` for ``token``.

    Construction does no I/O, so callers build fresh clients per use and read
    their token at call time (see ``runtime.workers_client``).
    """
    return AsyncWebClient(token=token)


def slack_error_code(exc: Exception) -> str | None:
    """Return the Slack API error code for ``exc``, or ``None``.

    ``None`` for anything that is not a ``SlackApiError`` (or a ``SlackApiError``
    carrying no response), so callers can classify errors with a plain membership
    test without importing ``slack_sdk`` themselves.
    """
    if isinstance(exc, SlackApiError) and getattr(exc, "response", None):
        return exc.response.get("error")
    return None
