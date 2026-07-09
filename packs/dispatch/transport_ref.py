"""Transport-neutral originator handle for dispatch workers (#663).

``TransportRef`` replaces the raw Slack triple (channel / thread_ts / agent)
at the dispatch boundary so workers can report status back to any transport
(Slack, Discord, or terminal).

Env vars
--------
DISPATCH_TRANSPORT
    "slack" | "discord" | "terminal".  Absent or blank → "slack" for
    backward-compatibility.

For the **slack** transport (default):
    DISPATCH_CHANNEL     — Slack channel ID (existing)
    DISPATCH_THREAD_TS   — Slack thread timestamp (existing)
    DISPATCH_AGENT       — logical agent name (existing)

For the **discord** transport:
    DISPATCH_CONVERSATION_ID
        Discord ConversationRef in the form
        ``"discord:<guild_id>:<channel_id>:<thread_id>"``.
        thread_id is "0" when the message is in the channel root.
    DISPATCH_AGENT       — logical agent name (same var)

For the **terminal** transport:
    DISPATCH_AGENT       — logical agent name (same var)

Unknown (non-blank) transport → ``ValueError`` (fail-closed, never a
silent no-op).

Discord status posting
----------------------
``_post_discord_message`` uses the bot token from the ``WORKERS_DISCORD_TOKEN``
env var.  When that token is absent the call logs a warning and returns
``False`` — the worker continues its GitHub work (degrade-not-crash contract).

Behind the ``DISCORD_WORKER_STATUS_VIA_AGENT`` flag (#707, default off),
``handler.py`` routes status posts through the router's ``/internal/status``
callback instead — see ``handler._post_discord_status_via_router``. This
module's ``_post_discord_message`` stays the flag-off, byte-for-byte path.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib import request as urlrequest
from urllib.error import URLError

logger = logging.getLogger("dispatch.transport_ref")

# ---------------------------------------------------------------------------
# Env-var names
# ---------------------------------------------------------------------------

DISPATCH_TRANSPORT_ENV = "DISPATCH_TRANSPORT"
DISPATCH_CONVERSATION_ID_ENV = "DISPATCH_CONVERSATION_ID"
DISPATCH_AGENT_ENV = "DISPATCH_AGENT"
# Slack-specific (existing vars, kept for backward-compat)
DISPATCH_CHANNEL_ENV = "DISPATCH_CHANNEL"
DISPATCH_THREAD_TS_ENV = "DISPATCH_THREAD_TS"
# Discord worker bot token (mirrors WORKERS_BOT_TOKEN for the Slack path)
WORKERS_DISCORD_TOKEN_ENV = "WORKERS_DISCORD_TOKEN"
# Flag (#707): when truthy, Discord worker status posts route through the
# router's /internal/status callback (dispatching agent's adapter identity)
# instead of a direct WORKERS_DISCORD_TOKEN REST call. Injected by
# router/packs/dispatch_hook.py from the DISCORD_WORKER_STATUS_VIA_AGENT
# settings-registry flag; default off, verbatim old behavior.
DISCORD_WORKER_STATUS_VIA_AGENT_ENV = "DISCORD_WORKER_STATUS_VIA_AGENT"

# ---------------------------------------------------------------------------
# Transport constants
# ---------------------------------------------------------------------------

TRANSPORT_SLACK = "slack"
TRANSPORT_DISCORD = "discord"
TRANSPORT_TERMINAL = "terminal"
_KNOWN_TRANSPORTS = frozenset({TRANSPORT_SLACK, TRANSPORT_DISCORD, TRANSPORT_TERMINAL})

DISCORD_API_BASE = "https://discord.com/api/v10"


# ---------------------------------------------------------------------------
# TransportRef dataclass
# ---------------------------------------------------------------------------


@dataclass
class TransportRef:
    """Transport-neutral originator handle.

    Attributes
    ----------
    transport:
        "slack" | "discord" | "terminal"
    conversation_id:
        Transport-specific location identifier.
        Slack   → channel ID (e.g. ``"C0123ABC456"``).
        Discord → ConversationRef string
                  ``"discord:<guild>:<channel>:<thread>"``.
        Terminal → empty string.
    thread_id:
        Transport-specific thread pointer.
        Slack   → ``thread_ts`` (e.g. ``"1717000000.000100"``).
        Discord → empty string (thread info is embedded in conversation_id).
        Terminal → empty string.
    agent:
        Logical agent name (e.g. ``"sam"`` / ``"lisa"``).
    """

    transport: str
    conversation_id: str
    thread_id: str
    agent: str


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_transport_ref(
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
    agent: str | None = None,
    conversation_id: str | None = None,
    transport: str | None = None,
    environ: dict[str, str] | None = None,
) -> TransportRef:
    """Resolve a :class:`TransportRef` from flags and environment variables.

    Flags win over env vars. Empty strings count as unset — a stray
    ``--channel ""`` flag surfaces a clear missing-context error rather
    than silently passing through.

    Transport selection
    -------------------
    1. ``transport`` arg or ``$DISPATCH_TRANSPORT`` env var (absent → "slack").
    2. Unknown transport → ``ValueError`` (fail-closed, never a silent no-op).

    Slack path (backward-compatible)
    ---------------------------------
    Reads ``DISPATCH_CHANNEL`` / ``DISPATCH_THREAD_TS`` / ``DISPATCH_AGENT``.
    Raises ``ValueError`` listing every missing field — identical behaviour to
    the old ``_resolve_slack_context`` so the Slack path is a regression-safe
    shim over this resolver.

    Discord path
    ------------
    Reads ``DISPATCH_CONVERSATION_ID`` and ``DISPATCH_AGENT``.

    Terminal path
    -------------
    Reads ``DISPATCH_AGENT`` only.
    """
    env = environ if environ is not None else os.environ

    raw_transport = transport or env.get(DISPATCH_TRANSPORT_ENV) or ""
    resolved_transport = raw_transport.lower() if raw_transport else TRANSPORT_SLACK

    if resolved_transport not in _KNOWN_TRANSPORTS:
        raise ValueError(f"unknown transport: {resolved_transport!r}; must be one of {sorted(_KNOWN_TRANSPORTS)}")

    resolved_agent = agent or env.get(DISPATCH_AGENT_ENV) or ""
    if not resolved_agent:
        raise ValueError(f"--agent or ${DISPATCH_AGENT_ENV} required")

    if resolved_transport == TRANSPORT_DISCORD:
        resolved_conv_id = conversation_id or env.get(DISPATCH_CONVERSATION_ID_ENV) or ""
        if not resolved_conv_id:
            raise ValueError(f"--conversation-id or ${DISPATCH_CONVERSATION_ID_ENV} required for discord transport")
        return TransportRef(
            transport=TRANSPORT_DISCORD,
            conversation_id=resolved_conv_id,
            thread_id="",
            agent=resolved_agent,
        )

    if resolved_transport == TRANSPORT_TERMINAL:
        return TransportRef(
            transport=TRANSPORT_TERMINAL,
            conversation_id="",
            thread_id="",
            agent=resolved_agent,
        )

    # Slack (default) — backward-compat with existing env triple.
    resolved_channel = channel or env.get(DISPATCH_CHANNEL_ENV) or ""
    resolved_thread_ts = thread_ts or env.get(DISPATCH_THREAD_TS_ENV) or ""

    missing: list[str] = []
    if not resolved_channel:
        missing.append(f"--channel or ${DISPATCH_CHANNEL_ENV}")
    if not resolved_thread_ts:
        missing.append(f"--thread-ts or ${DISPATCH_THREAD_TS_ENV}")
    if missing:
        raise ValueError("missing required Slack context: " + ", ".join(missing))

    return TransportRef(
        transport=TRANSPORT_SLACK,
        conversation_id=resolved_channel,
        thread_id=resolved_thread_ts,
        agent=resolved_agent,
    )


# ---------------------------------------------------------------------------
# Discord REST poster
# ---------------------------------------------------------------------------


def _post_discord_message(
    *,
    conversation_ref: str,
    text: str,
    token: str | None,
    dispatch_id: str = "",
    persona: str = "",
    _urlopen: Any = None,
) -> bool:
    """Post *text* to a Discord channel/thread via the REST API.

    conversation_ref format: ``"discord:<guild_id>:<channel_id>:<thread_id>"``
    When thread_id is "0" the message goes to the channel root; otherwise it
    goes to the thread (Discord threads are addressable as channels).

    Returns ``True`` on success, ``False`` on any error (including missing
    token) — callers must not rely on this raising.
    """
    if not token:
        logger.warning(
            "post_status: %s not set — Discord post skipped (dispatch=%s)",
            WORKERS_DISCORD_TOKEN_ENV,
            dispatch_id,
        )
        return False

    try:
        body = conversation_ref.removeprefix("discord:")
        parts = body.split(":")
        if len(parts) != 3:  # noqa: PLR2004
            raise ValueError("wrong number of parts")
        _guild_id, channel_id, thread_id = parts
    except (ValueError, AttributeError):
        logger.warning(
            "post_status: malformed Discord conversation_ref %r (dispatch=%s)",
            conversation_ref,
            dispatch_id,
        )
        return False

    # When thread_id is non-zero, messages are posted to the thread channel directly.
    target_channel = thread_id if thread_id != "0" else channel_id

    prefix = f"[{dispatch_id} · {persona}] " if dispatch_id and persona else ""
    payload: dict[str, Any] = {"content": prefix + text}

    url = f"{DISCORD_API_BASE}/channels/{target_channel}/messages"
    data = json.dumps(payload).encode()
    req = urlrequest.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {token}",
        },
    )
    opener = _urlopen or urlrequest.urlopen
    try:
        opener(req, timeout=5)
        return True
    except (URLError, OSError) as exc:
        logger.warning(
            "post_status: Discord post failed (channel=%s dispatch=%s): %s",
            target_channel,
            dispatch_id,
            exc,
        )
        return False
