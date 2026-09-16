"""Boundary ratchet: ``slack_sdk`` is only imported under the adapter package (#553).

The Slack transport SDK is a Slack-ism. Keeping it quarantined under
``router/chat/adapters/`` is what lets core stay transport-neutral — core talks
to the :class:`~router.chat.interface.ChatAdapter` contract, never to a raw
``slack_sdk`` client. The single core-facing seam is
``router.chat.adapters.slack_client``, which re-exports the client type and
error helpers so non-adapter modules never need to import ``slack_sdk`` directly.
Since the only source of a raw ``AsyncWebClient`` is a ``slack_sdk`` import, the
same guard covers "raw ``AsyncWebClient``" (#857) with no extra check needed.

This test is the CI grep-guard for that boundary. If it fails on your change:
route the call through ``router.chat.adapters`` (or the ``slack_client`` seam)
instead of importing ``slack_sdk`` in core.

Docstring/comment *mentions* of ``slack_sdk`` are fine — only real ``import``
statements are policed.

#857 extends the same ratchet to the ``slack_post`` best-effort-post primitive
(``router/chat/adapters/slack_post.py``, relocated from ``router/slack_post.py``
by that issue): non-adapter modules with a legitimate need for it (the
permanent Slack-fallback posters in ``auto_dispatch/notify.py``,
``dispatch/feed_transport.py``, ``kill_command.py``) import it *through* the
adapter package (``from router.chat.adapters import slack_post``); nothing may
resurrect it at the old top-level ``router.slack_post`` path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ROUTER_DIR = REPO_ROOT / "router"

# The only package allowed to import slack_sdk directly.
ALLOWED_PREFIX = ROUTER_DIR / "chat" / "adapters"

# Match real import statements only ("import slack_sdk" / "from slack_sdk..."),
# not docstring or comment mentions.
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+slack_sdk\b", re.MULTILINE)

# Match a top-level ``router.slack_post`` import (the pre-#857 location) —
# not the current ``from router.chat.adapters import slack_post`` seam import.
_SLACK_POST_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+router\s+import\s+(?:[\w, ]*,\s*)?slack_post\b"
    r"|from\s+router\.slack_post\s+import"
    r"|import\s+router\.slack_post\b)",
    re.MULTILINE,
)


def _router_py_files() -> list[Path]:
    return sorted(ROUTER_DIR.rglob("*.py"))


def test_slack_sdk_imported_only_under_adapters() -> None:
    """No module outside ``router/chat/adapters/`` imports ``slack_sdk``."""
    offenders: list[str] = []
    for path in _router_py_files():
        if path.is_relative_to(ALLOWED_PREFIX):
            continue
        source = path.read_text(encoding="utf-8")
        if _IMPORT_RE.search(source):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, (
        "slack_sdk may only be imported under router/chat/adapters/ (#553). "
        "Route through router.chat.adapters.slack_client instead. Offenders: " + ", ".join(offenders)
    )


def test_slack_post_module_lives_under_adapters() -> None:
    """The ``slack_post`` primitive was relocated under ``router/chat/adapters/`` (#857)."""
    assert (ALLOWED_PREFIX / "slack_post.py").is_file()
    assert not (ROUTER_DIR / "slack_post.py").exists()


def test_slack_post_imported_only_via_adapters_path() -> None:
    """No module imports ``slack_post`` at the old top-level ``router.slack_post`` path (#857)."""
    offenders: list[str] = []
    for path in _router_py_files():
        source = path.read_text(encoding="utf-8")
        if _SLACK_POST_IMPORT_RE.search(source):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, (
        "slack_post must be imported via router.chat.adapters (#857), never the old "
        "top-level router.slack_post path. Offenders: " + ", ".join(offenders)
    )


def test_slack_client_seam_exposes_the_boundary() -> None:
    """The seam module exports the symbols core relies on."""
    from router.chat.adapters import slack_client

    for name in ("AsyncWebClient", "SlackApiError", "slack_error_code"):
        assert hasattr(slack_client, name), f"slack_client is missing {name!r}"


def test_slack_error_code_reads_slack_api_errors() -> None:
    """``slack_error_code`` extracts the code from a ``SlackApiError``."""
    from router.chat.adapters.slack_client import SlackApiError, slack_error_code

    exc = SlackApiError("boom", response={"error": "is_archived"})
    assert slack_error_code(exc) == "is_archived"


def test_slack_error_code_returns_none_for_non_slack_errors() -> None:
    """Non-Slack exceptions (and response-less ones) classify as ``None``."""
    from router.chat.adapters.slack_client import SlackApiError, slack_error_code

    assert slack_error_code(ValueError("nope")) is None
    assert slack_error_code(SlackApiError("boom", response=None)) is None
