"""Process-wide runtime registries shared across the router's modules.

``router.app`` is the composition root: it builds the per-agent Bolt apps and
resolves bot user IDs at startup, then populates these registries. The event
pipeline (``router.slack_events``), the approved-draft executor
(``router.approvals.execute``) and the session lifecycle loops
(``router.session_lifecycle``) read them at call time.

All collections are mutated **in place** (never rebound) so that aliases —
including the back-compat ``router.app._<name>`` aliases used by tests —
always observe the live state. The one scalar (``workers_bot_user_id``) is
read via ``runtime.workers_bot_user_id`` attribute access at call time for
the same reason.
"""

from __future__ import annotations

from typing import Any

from router import settings
from router.chat.adapters.slack_client import AsyncWebClient

# One AsyncApp per agent with configured Slack credentials, keyed by agent
# name. Socket Mode app tokens live beside them.
apps_by_agent: dict[str, Any] = {}
app_tokens_by_agent: dict[str, str] = {}

# Agent name → resolved bot user ID (from auth.test), and the reverse map
# (bot user ID → agent name) used by mention parsing in handoff detection.
bot_user_id_by_agent: dict[str, str] = {}
bot_user_map: dict[str, str] = {}

# Allowlist of Slack user IDs (U…) for bots that may trigger auto-review.
# Populated at startup from two sources:
#   1. The DISPATCH_BOT_USER_IDS environment variable (comma-separated list)
#      — for external bots / future machine-user identities (#199/#227).
#   2. Each agent's own resolved bot user ID (added after auth.test) — so the
#      supervisor's auto-review handoff, which posts via the receiving agent's
#      own bolt client, isn't dropped by the bot-message guard.
# A loop is theoretically possible if an agent's normal response text contains
# ``<@self>``, but in practice agents do not self-mention outside the
# supervisor handoff. Revisit once dedicated machine-user PATs land (#227);
# at that point the supervisor will post under its own identity and the
# self-id auto-seed can be removed.
dispatch_bot_user_ids: set[str] = set()

# Resolved user ID of the workers bot (issue #283). Stored separately so the
# @mention carve-out can gate on workers-bot identity without widening the
# general allowlist check.
workers_bot_user_id: str | None = None

# Strong references to Discord adapter instances built in main() behind
# DISCORD_ENABLED. Parked at module level so the event loop doesn't drop them
# while the asyncio.gather in main() is running.
discord_adapters: list = []


def workers_client() -> AsyncWebClient | None:
    """Return an ``AsyncWebClient`` authenticated as the workers bot, or None.

    The workers app (``WORKERS_BOT_TOKEN``) is the single runtime identity that
    speaks for posts reporting *on a dispatch* — lifecycle acks, slot tracker,
    auto-review handoff (issue #270). Lifecycle acks routed through whichever
    agent's bolt client handled the approval click would render as that agent's
    persona and (for the done-post) ``@``-mention the agent into a self-loop.

    Safe-degrades to ``None`` when the token is unset, so callers fall back to
    the agent client — i.e. exactly today's behaviour in a deployment that has
    not configured the workers app yet. Builds a fresh client per call (cheap:
    construction does no I/O), reading the token at call time so a late-injected
    token is honoured without a restart.
    """
    token = settings.get("WORKERS_BOT_TOKEN")
    if not token:
        return None
    return AsyncWebClient(token=token)


def client_for_agent(agent_name: str) -> Any | None:
    """Return the Slack WebClient for ``agent_name``, or None if not configured."""
    bolt_app = apps_by_agent.get(agent_name)
    return bolt_app.client if bolt_app else None


def discord_adapter_for_agent(agent_name: str) -> Any | None:
    """Return the running ``DiscordAdapter`` for ``agent_name``, or None if none is active."""
    return next((a for a in discord_adapters if a.agent_name == agent_name), None)
