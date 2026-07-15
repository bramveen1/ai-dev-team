"""Workspace user directory — display-name → Slack user ID resolution.

Backs outbound @mention linkification: agents write plain-text mentions
("@lisa", "@Dev Lisa", "@Bram") because they never see Slack user IDs; the
router rewrites them to real ``<@UID>`` mentions at the posting boundary so
they render as clickable mentions.

The directory is built from ``users.list`` (needs the ``users:read`` bot
scope) and cached module-wide with a TTL. Persona agents are overlaid from
``runtime.bot_user_id_by_agent`` — their logical names ("lisa") and manifest
display names ("Lisa") resolve even when ``users.list`` is unavailable, and
they win over workspace entries on name collisions.

Failure is always soft: a missing scope, a network error, or a mock client
in tests yields an empty workspace directory (logged once), and mention
rewriting simply leaves unresolved names as plain text.
"""

from __future__ import annotations

import logging
import time

from router import runtime
from router.config import get_agent_map

logger = logging.getLogger(__name__)

_TTL_SECONDS = 15 * 60
# users.list pagination guard — 10 pages × 200 members is far beyond the
# workspaces this router serves; prevents a runaway cursor loop.
_MAX_PAGES = 10
_PAGE_LIMIT = 200
# Ignore one-character names ("a", "-") — they'd turn ordinary prose into
# mentions.
_MIN_NAME_LEN = 2

_cache: dict[str, str] = {}
_cache_at: float = 0.0
_warned_unavailable = False


def _remember(directory: dict[str, str], name: str | None, uid: str) -> None:
    """Record ``name → uid`` (lower-cased) unless the name is unusable or taken."""
    if not name or len(name) < _MIN_NAME_LEN:
        return
    directory.setdefault(name.strip().lower(), uid)


async def workspace_user_ids(client) -> dict[str, str]:
    """Return the cached ``lower-case name → user ID`` map from ``users.list``.

    Handles, display names, and real names all resolve. Deleted users are
    skipped; bots are kept (persona bots' workspace display names — e.g.
    "Dev Lisa" — are how agents mention each other). Returns the last-good
    map when the API is unavailable and the cache is merely stale, or ``{}``
    when it never succeeded.
    """
    global _cache_at, _warned_unavailable
    now = time.monotonic()
    if _cache and now - _cache_at < _TTL_SECONDS:
        return _cache

    fresh: dict[str, str] = {}
    cursor = ""
    try:
        for _ in range(_MAX_PAGES):
            resp = await client.users_list(cursor=cursor or None, limit=_PAGE_LIMIT)
            # SlackResponse exposes the payload as .data; only trust real
            # dicts — mock clients (unit tests) return mock objects whose
            # attribute access would fabricate more mocks, not data.
            data = resp if isinstance(resp, dict) else getattr(resp, "data", None)
            if not isinstance(data, dict):
                break
            for member in data.get("members") or []:
                if not isinstance(member, dict) or member.get("deleted"):
                    continue
                uid = member.get("id", "")
                if not uid:
                    continue
                profile = member.get("profile") or {}
                _remember(fresh, profile.get("display_name"), uid)
                _remember(fresh, profile.get("real_name"), uid)
                _remember(fresh, member.get("name"), uid)
            meta = data.get("response_metadata")
            cursor = meta.get("next_cursor", "") if isinstance(meta, dict) else ""
            if not cursor:
                break
    except Exception:
        if not _warned_unavailable:
            logger.warning(
                "users.list unavailable (missing users:read scope?) — outbound mentions resolve persona agents only",
                exc_info=True,
            )
            _warned_unavailable = True
        return dict(_cache)

    _cache.clear()
    _cache.update(fresh)
    _cache_at = now
    _warned_unavailable = False
    return _cache


async def outbound_mention_ids(client) -> dict[str, str]:
    """Directory for outbound mention rewriting: workspace users + personas.

    Persona agents overlay the workspace map so their logical names and
    manifest display names always resolve to the persona bot's user ID.
    """
    directory = dict(await workspace_user_ids(client))
    agent_map = get_agent_map()
    for agent_name, uid in runtime.bot_user_id_by_agent.items():
        for name in (agent_name, agent_map.get(agent_name, {}).get("name", "")):
            if name and len(name) >= _MIN_NAME_LEN:
                directory[name.lower()] = uid
    return directory
