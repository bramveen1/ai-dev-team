"""Background session-lifecycle loops: timeout cleanup and draft expiration.

Started by ``router.app.main`` as long-lived tasks. Transport resolution for
session-end posts lives here too — Discord-transport sessions post through
their adapter, everything else through the agent's Slack client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from router import runtime
from router.config import get_agent_map
from router.runtime import client_for_agent as _client_for_agent
from router.session_end import handle_timeout_exit
from router.session_manager import pop_timed_out_sessions

logger = logging.getLogger(__name__)

# Router config dict, injected by ``router.app`` at import (and again on
# reload) via :func:`configure` so both modules share one loaded config.
config: dict = {}


def configure(router_config: dict) -> None:
    """Inject the loaded router config (called by ``router.app``)."""
    global config
    config = router_config


class _DiscordSessionClient:
    """Minimal ``chat_postMessage``-shaped facade over a DiscordAdapter.

    Lets transport-agnostic session-end code (``handle_timeout_exit``) post the
    timeout summary into the originating Discord conversation without knowing
    about the adapter contract. Extra Slack-only kwargs (``metadata``) are
    accepted and dropped — provenance on Discord is re-derived at read time
    from bot authorship (see DiscordAdapter.read_thread).
    """

    def __init__(self, adapter: Any, conversation_ref: str) -> None:
        self._adapter = adapter
        self._ref = conversation_ref

    async def chat_postMessage(self, *, channel: str = "", thread_ts: str = "", text: str = "", **_kwargs) -> dict:
        from router.chat.types import ConversationRef, OutboundMessage

        await self._adapter.send_message(OutboundMessage(text=text, conversation_ref=ConversationRef(self._ref)))
        return {"ok": True}


def _session_end_client(session: dict) -> Any | None:
    """Resolve the client used to post this session's timeout summary.

    Discord-transport sessions (tagged by the Discord adapter at creation)
    post through their adapter; everything else keeps today's Slack client.
    """
    agent_name = session["agent_name"]
    if session.get("transport") == "discord":
        adapter = next((a for a in runtime.discord_adapters if a.agent_name == agent_name), None)
        conversation_ref = session.get("conversation_ref")
        if adapter is None or not conversation_ref:
            return None
        return _DiscordSessionClient(adapter, conversation_ref)
    return _client_for_agent(agent_name)


async def _session_cleanup_loop(interval_seconds: int = 60) -> None:
    """Periodically clean up timed-out sessions and post summaries."""
    agent_map = get_agent_map()
    logger.info("Session cleanup loop started (interval=%ds)", interval_seconds)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            expired = pop_timed_out_sessions(config.get("session_timeout"))
            for session in expired:
                agent_name = session["agent_name"]
                agent_config = agent_map.get(agent_name)
                if not agent_config:
                    logger.warning("No agent config for %s, skipping timeout exit", agent_name)
                    continue

                slack_client = _session_end_client(session)
                if slack_client is None:
                    logger.warning("No transport client for %s, skipping timeout exit", agent_name)
                    continue

                try:
                    await handle_timeout_exit(
                        agent_name=agent_name,
                        container=agent_config["container"],
                        thread_history=session.get("thread_history", []),
                        slack_client=slack_client,
                        channel=session["channel"],
                        thread_ts=session["thread_ts"],
                    )
                except Exception:
                    logger.exception("Error during timeout exit for session %s", session["session_id"])

            if expired:
                logger.info("Cleaned up %d timed-out sessions", len(expired))
        except Exception:
            logger.exception("Error during session cleanup")


async def _expiration_worker_loop(store: Any, interval_seconds: int = 3600) -> None:
    """Periodically run the draft expiration worker against *store*."""
    from router.approvals.expiration_worker import run_once

    logger.info("Draft expiration worker started (interval=%ds)", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            counts = await run_once(store=store, client_resolver=_client_for_agent)
            total = sum(counts.values())
            if total:
                logger.info("Expiration worker: %s", counts)
        except Exception:
            logger.exception("Error in expiration worker")
