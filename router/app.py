"""Router app — main slack_bolt application for the multi-agent system.

Receives Slack events (app_mention, DM messages) and dispatches them
to the appropriate agent container via the dispatcher module.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from router.config import get_agent_map, load_config
from router.dispatcher import dispatch
from router.session_end import handle_clean_exit, is_exit_trigger
from router.session_manager import (
    cleanup_timed_out_sessions,
    create_session,
    find_session_by_thread,
    update_activity,
)

load_dotenv()

config = load_config()

logging.basicConfig(
    level=getattr(logging, config["log_level"], logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_bolt_logger = logging.getLogger("slack_bolt")
_bolt_logger.setLevel(logging.INFO)
app = AsyncApp(
    token=config["slack_bot_token"],
    signing_secret=config["slack_signing_secret"],
    logger=_bolt_logger,
)

# Bot user ID → agent name mapping, populated at startup
_bot_user_map: dict[str, str] = {}


def _resolve_agent(event: dict) -> str | None:
    """Determine which agent should handle this event.

    For Phase 1 we default to 'lisa' since she is the only agent.
    Future: look up the bot_user_id from the event to identify the target agent.
    """
    # Check if a specific bot was mentioned via bot_user_map
    text = event.get("text", "")
    for bot_user_id, agent_name in _bot_user_map.items():
        if f"<@{bot_user_id}>" in text:
            return agent_name

    # Default to lisa for Phase 1
    return "lisa"


async def _handle_event(event: dict, say, client) -> None:
    """Common handler for app_mention and message events."""
    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    event_type = event.get("type", "unknown")

    logger.info(
        "Received event type=%s channel=%s user=%s thread_ts=%s text=%s",
        event_type,
        channel,
        user,
        thread_ts,
        text[:80] if text else "",
    )

    # Ignore bot messages to avoid loops
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        logger.debug("Ignoring bot message")
        return

    agent_name = _resolve_agent(event)
    if agent_name is None:
        logger.warning("Could not resolve agent for event in channel=%s", channel)
        return

    agent_map = get_agent_map()
    if agent_name not in agent_map:
        logger.error("Agent %s not found in agent map", agent_name)
        return

    # Create or update session
    session = create_session(channel=channel, thread_ts=thread_ts, agent_name=agent_name)
    logger.debug("Session %s active for agent=%s", session["session_id"], agent_name)

    # Check for clean exit trigger
    if is_exit_trigger(text):
        logger.info("Exit trigger detected in thread=%s from user=%s", thread_ts, user)
        agent_config = agent_map[agent_name]
        try:
            await handle_clean_exit(
                agent_name=agent_name,
                container=agent_config["container"],
                thread_history=[],  # Thread history loading is added in #11
                slack_client=client,
                channel=channel,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Error during clean exit for agent %s", agent_name)
        await say(text="You're welcome! I've saved our conversation notes.", thread_ts=thread_ts)
        return

    # Add a thinking reaction
    try:
        await client.reactions_add(channel=channel, name="eyes", timestamp=event.get("ts", ""))
    except Exception:
        logger.debug("Could not add reaction (non-critical)")

    # Dispatch to agent
    try:
        result = await dispatch(
            agent_name=agent_name,
            message=text,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config["session_timeout"],
            max_token_budget=config["max_token_budget"],
        )

        update_activity(session["session_id"])

        # Reply in-thread
        await say(text=result["response"], thread_ts=thread_ts)
        logger.info("Responded in thread=%s agent=%s", thread_ts, agent_name)

    except Exception:
        logger.exception("Error dispatching to agent %s", agent_name)
        await say(text="Sorry, something went wrong while processing your request.", thread_ts=thread_ts)


@app.event("app_mention")
async def handle_app_mention(event, say, client):
    """Handle @mentions of the bot in channels."""
    await _handle_event(event, say, client)


@app.event("message")
async def handle_message(event, say, client):
    """Handle direct messages and thread follow-ups to the bot."""
    channel_type = event.get("channel_type", "")

    # Always handle DMs
    if channel_type == "im":
        await _handle_event(event, say, client)
        return

    # In channels, handle thread replies where the bot has an active session
    thread_ts = event.get("thread_ts")
    if thread_ts:
        channel = event.get("channel", "")
        session = find_session_by_thread(channel, thread_ts)
        if session:
            await _handle_event(event, say, client)
            return


def _start_session_cleanup_timer(interval_seconds: int = 60) -> None:
    """Start a background thread that periodically cleans up timed-out sessions."""

    def _cleanup_loop():
        while True:
            try:
                count = cleanup_timed_out_sessions(config["session_timeout"])
                if count > 0:
                    logger.info("Cleaned up %d timed-out sessions", count)
            except Exception:
                logger.exception("Error during session cleanup")
            threading.Event().wait(interval_seconds)

    thread = threading.Thread(target=_cleanup_loop, daemon=True, name="session-cleanup")
    thread.start()
    logger.info("Session cleanup timer started (interval=%ds)", interval_seconds)


async def main():
    """Start the router in Socket Mode."""
    logger.info("Starting router service...")
    _start_session_cleanup_timer()

    handler = AsyncSocketModeHandler(app, config["slack_app_token"])
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
