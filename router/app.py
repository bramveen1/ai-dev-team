"""Router app — main slack_bolt application for the multi-agent system.

Receives Slack events (app_mention, DM messages) and dispatches them
to the appropriate agent container via the dispatcher module.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from router.approvals.capabilities_loader import get_capability_instance
from router.approvals.handlers import register_handlers
from router.approvals.interceptor import parse_response, post_approval_message
from router.approvals.store import DraftStore
from router.config import get_agent_map, load_config
from router.dispatcher import dispatch
from router.memory_curator import curate_agent_memory, needs_curation
from router.session_end import handle_clean_exit, handle_timeout_exit, is_exit_trigger
from router.session_manager import (
    add_to_thread_history,
    create_session,
    find_session_by_thread,
    pop_timed_out_sessions,
    update_activity,
)
from router.slack_format import md_to_slack

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

# --- Approval flow setup ---
_draft_store = DraftStore()
register_handlers(app, _draft_store)

# Bot user ID → agent name mapping, populated at startup
_bot_user_map: dict[str, str] = {}

# Bot's own user ID, populated at startup
_bot_user_id: str | None = None


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


DEFAULT_THINKING_STATUS = "is thinking\u2026"


async def set_assistant_status(client, channel: str, thread_ts: str, status: str) -> None:
    """Set the assistant thread status indicator (auto-clears on next message).

    Uses the Slack assistant.threads.setStatus API which renders as
    "<App Name> <status>" beneath the bot's name in the thread.
    The status auto-clears when the bot posts a message or after 2 minutes.
    """
    try:
        await client.assistant_threads_setStatus(
            channel_id=channel,
            thread_ts=thread_ts,
            status=status,
        )
    except Exception:
        logger.debug("Could not set assistant status (non-critical)")


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

    # Find existing session or create a new one
    session = find_session_by_thread(channel, thread_ts)
    if session is None:
        session = create_session(channel=channel, thread_ts=thread_ts, agent_name=agent_name)
        logger.debug("Created session %s for agent=%s", session["session_id"], agent_name)
    else:
        update_activity(session["session_id"])
        logger.debug("Reusing session %s for agent=%s", session["session_id"], agent_name)

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

    # Trigger background memory curation if needed (first message of the day)
    agent_config = agent_map[agent_name]
    if needs_curation(agent_name):
        logger.info("Triggering background memory curation for %s", agent_name)
        asyncio.create_task(curate_agent_memory(agent_name, agent_config["container"]))

    # Show assistant status indicator while the agent works
    thinking_text = agent_config.get("thinking_status", DEFAULT_THINKING_STATUS)
    await set_assistant_status(client, channel, thread_ts, thinking_text)

    # Record the user's message in session history
    add_to_thread_history(session["session_id"], {"user": user, "text": text})

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

        # Check for draft-approval blocks in the response
        intercept = parse_response(result["response"])

        # Record the agent's response in session history (use cleaned text)
        response_text = intercept.cleaned_text if intercept.has_drafts else result["response"]
        add_to_thread_history(session["session_id"], {"user": agent_name, "text": response_text})

        # Post the agent's text response (cleaned of approval blocks)
        if response_text:
            await say(text=md_to_slack(response_text), thread_ts=thread_ts)

        # Post approval messages for any draft-approval blocks
        for draft_req in intercept.draft_requests:
            cap_instance = get_capability_instance(
                agent_name=agent_name,
                capability_type=draft_req.capability_type,
                instance_name=draft_req.capability_instance,
            )
            try:
                await post_approval_message(
                    draft_request=draft_req,
                    agent_name=agent_name,
                    channel=channel,
                    thread_ts=thread_ts,
                    client=client,
                    store=_draft_store,
                    capability_instance=cap_instance,
                )
            except Exception:
                logger.exception("Failed to post approval message for draft %s", draft_req.draft_id)

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
    # Skip messages that @mention the bot — those are already handled by app_mention
    if _bot_user_id and f"<@{_bot_user_id}>" in event.get("text", ""):
        return

    thread_ts = event.get("thread_ts")
    if thread_ts:
        channel = event.get("channel", "")
        session = find_session_by_thread(channel, thread_ts)
        if session:
            await _handle_event(event, say, client)
            return


async def _session_cleanup_loop(interval_seconds: int = 60) -> None:
    """Periodically clean up timed-out sessions and post summaries."""
    agent_map = get_agent_map()
    logger.info("Session cleanup loop started (interval=%ds)", interval_seconds)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            expired = pop_timed_out_sessions(config["session_timeout"])
            for session in expired:
                agent_name = session["agent_name"]
                agent_config = agent_map.get(agent_name)
                if not agent_config:
                    logger.warning("No agent config for %s, skipping timeout exit", agent_name)
                    continue

                try:
                    await handle_timeout_exit(
                        agent_name=agent_name,
                        container=agent_config["container"],
                        thread_history=session.get("thread_history", []),
                        slack_client=app.client,
                        channel=session["channel"],
                        thread_ts=session["thread_ts"],
                    )
                except Exception:
                    logger.exception("Error during timeout exit for session %s", session["session_id"])

            if expired:
                logger.info("Cleaned up %d timed-out sessions", len(expired))
        except Exception:
            logger.exception("Error during session cleanup")


async def _expiration_worker_loop(interval_seconds: int = 3600) -> None:
    """Periodically run the draft expiration worker."""
    from router.approvals.expiration_worker import run_once

    logger.info("Draft expiration worker started (interval=%ds)", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            counts = await run_once(store=_draft_store, client=app.client)
            total = sum(counts.values())
            if total:
                logger.info("Expiration worker: %s", counts)
        except Exception:
            logger.exception("Error in expiration worker")


async def main():
    """Start the router in Socket Mode."""
    global _bot_user_id
    logger.info("Starting router service...")

    # Resolve the bot's own user ID so we can deduplicate events
    try:
        auth_resp = await app.client.auth_test()
        _bot_user_id = auth_resp["user_id"]
        logger.info("Bot user ID: %s", _bot_user_id)
    except Exception:
        logger.warning("Could not resolve bot user ID via auth.test")

    asyncio.create_task(_session_cleanup_loop())
    asyncio.create_task(_expiration_worker_loop())

    handler = AsyncSocketModeHandler(app, config["slack_app_token"])
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
