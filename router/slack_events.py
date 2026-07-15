"""Inbound Slack event pipeline — routing, dedup, guards, and dispatch.

Owns everything that happens between "a Bolt app received an event" and "the
agent's response was posted back to the thread": the bot-message guard and
dispatch-bot allowlist, event deduplication, thread-state bookkeeping, pack
command short-circuits, session management, attachment ingest, dispatch to
the agent CLI, and agent-handoff detection.

``router.app`` (the composition root) builds the per-agent handlers via
:func:`_make_event_handlers` and injects the loaded router config through
:func:`configure`. Shared bot-identity registries live in ``router.runtime``.
"""

from __future__ import annotations

import collections
import logging
import re

from router import attachments as attachments_mod
from router import background, runtime, settings
from router.attachments import (
    attachments_enabled,
    build_attachments_block,
    ingest_files,
    validate_files,
)
from router.chat import inbound_common
from router.chat.adapters.slack import SlackAdapter
from router.chat.adapters.slack import make_inbound_ref as slack_make_inbound_ref
from router.chat.core import InboundFacts, handle_inbound
from router.config import get_agent_map
from router.dispatch import state as _dstate
from router.dispatcher import dispatch
from router.error_classifier import build_error_message, make_correlation_id
from router.memory_curator import curate_agent_memory, is_curation_in_flight, needs_curation
from router.mentions import last_mentioned
from router.packs.grants import maybe_handle_pack_command, resolve_pending_reply
from router.session_end import handle_clean_exit, is_exit_trigger
from router.session_manager import (
    add_to_thread_history,
    cleanup_session,
    create_session,
    find_session_by_thread,
    update_activity,
)
from router.slack_format import md_to_slack
from router.slack_users import outbound_mention_ids
from router.thread_loader import SUMMARY_MARKERS
from router.threads.state import get_default_store

logger = logging.getLogger(__name__)

_spawn_background_task = background.spawn_background_task

# Router config dict, injected by ``router.app`` at import (and again on
# reload) via :func:`configure` so both modules share one loaded config.
config: dict = {}


def configure(router_config: dict) -> None:
    """Inject the loaded router config (called by ``router.app``)."""
    global config
    config = router_config


# ── Event deduplication ──────────────────────────────────────────────────────
# Slack can deliver the same user message via multiple event types (e.g. both
# ``app_mention`` and ``message`` for a human @-mention).  We guard against
# the resulting double-dispatch by remembering the identity of each event we
# have already started processing.
#
# Key: ``client_msg_id`` when present (set for human messages, stable across
# both event types for the same source); fallback to ``(channel, user, ts)``
# for events that lack it (e.g. bot messages that bypass the bot guard).
#
# Store: ``OrderedDict[key, expiry_epoch]`` capped at ``_SEEN_EVENTS_MAX``
# entries.  We evict by FIFO (oldest insertion) when the cap is reached and
# by TTL on each lookup.  In-process global; keyed per-agent so that a
# multi-mention (@sam @lisa) does not cause one agent to drop another's event.
# The algorithm lives in router.chat.inbound_common (shared with Discord);
# the store stays module-level so tests keep clearing it directly.
_SEEN_EVENTS_MAX: int = inbound_common.SEEN_EVENTS_MAX
_SEEN_EVENTS_TTL: float = inbound_common.SEEN_EVENTS_TTL
_seen_events: collections.OrderedDict[tuple, float] = collections.OrderedDict()


DEFAULT_THINKING_STATUS = "is thinking…"

_ATTACHMENTS_ROOT = attachments_mod._ATTACHMENTS_ROOT


def _make_event_handlers(agent_name: str):
    """Build per-agent app_mention / message handlers.

    Slack Bolt inspects handler signatures and only injects arguments it
    recognizes (event, say, client, body, ...). Capturing ``agent_name``
    via a factory closure (rather than a default arg) keeps the Bolt-facing
    signature clean — otherwise Bolt logs "<arg> is not a valid argument"
    and leaves the parameter unbound.
    """

    async def on_app_mention(event, say, client):
        await _handle_event(event, say, client, receiving_agent=agent_name, was_mentioned=True)

    async def on_message(event, say, client):
        await handle_message(event, say, client, receiving_agent=agent_name)

    return on_app_mention, on_message


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


def _has_persona_mention(text: str) -> bool:
    """Return True iff *text* contains a Slack mention token for a known persona bot.

    Scans for ``<@Uxxxxx>`` tokens and checks each extracted user ID against
    ``runtime.bot_user_id_by_agent.values()`` — the set of user IDs that belong
    to configured agent personas (sam-bot, lisa-bot, etc.).  The workers-bot
    user ID is intentionally excluded because ``bot_user_id_by_agent`` only
    holds persona bots registered via agent Bolt apps.
    """
    persona_uids = set(runtime.bot_user_id_by_agent.values())
    for uid in re.findall(r"<@(U[A-Z0-9_]+)>", text):
        if uid in persona_uids:
            return True
    return False


def _is_dispatch_bot_sender(event: dict, receiving_agent: str) -> bool:
    """Return True iff this bot event originates from a whitelisted dispatch-bot user.

    Whitelisted user IDs come from two sources, merged at startup:
      • The ``DISPATCH_BOT_USER_IDS`` environment variable (external bots).
      • Each agent's own resolved bot user ID, auto-seeded after ``auth.test``.

    The auto-seed is what lets the supervisor's auto-review handoff (posted
    via the receiving agent's own bolt client) get through the bot-message
    guard. ``receiving_agent`` is accepted for symmetry with the call site
    but is not used here — the allowlist is global, since one agent's
    supervisor may legitimately ping another agent's app.

    Workers-bot receives special treatment (issue #283): even though its user
    ID is in the allowlist, it is only allowed through when the
    ``WORKER_MENTION_HANDOFF`` env flag is ``"1"`` *and* the message text
    contains an explicit mention of a known persona bot.  This prevents
    un-mentioned thread routing and echo loops while still enabling the
    worker→agent handoff path.
    """
    sender = event.get("user", "")
    if not sender:
        return False
    if runtime.workers_bot_user_id and sender == runtime.workers_bot_user_id:
        if not settings.get("WORKER_MENTION_HANDOFF"):
            return False
        text = event.get("text", "") or ""
        return _has_persona_mention(text)
    return sender in runtime.dispatch_bot_user_ids


def _bump_attachment_thread_mtime(thread_ts: str) -> None:
    """Touch the per-thread attachments dir to refresh its GC TTL (#328/#330).

    Shared logic lives in :mod:`router.chat.inbound_common`; this wrapper
    forwards the module's attachments root so tests can keep steering it.
    """
    inbound_common.bump_attachment_thread_mtime(thread_ts, attachments_root=_ATTACHMENTS_ROOT)


async def _handle_event_via_adapter(
    event,
    client,
    *,
    receiving_agent: str,
    channel: str,
    thread_ts: str,
    text: str,
    user: str,
    was_mentioned: bool,
) -> None:
    """#553 adapter path: decode the Bolt event and hand it to the shared orchestrator.

    The Slack-specific gates (bot guard, dedup, thread-state recording, pack
    short-circuits) already ran in ``_handle_event``; this function owns only
    the decode into :class:`InboundFacts` and the attachment-ingest closure —
    kept in this module so the ``validate_files``/``ingest_files`` test seams
    keep intercepting.
    """
    adapter = SlackAdapter(receiving_agent, client)
    conversation_ref = slack_make_inbound_ref(channel, thread_ts)
    author_is_bot = bool(event.get("bot_id") or event.get("subtype") == "bot_message")

    ingest = None
    raw_files = event.get("files") or []
    if attachments_enabled() and thread_ts and raw_files:
        agent_creds = config.get("slack_credentials", {}).get(receiving_agent, {})
        bot_token = agent_creds.get("bot_token", "")

        async def _post_attachment_notice(notice: str) -> None:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=notice)

        async def ingest() -> tuple[list[str], bool]:
            return await inbound_common.ingest_attachments(
                raw_files,
                thread_ts,
                bot_token,
                attachments_root=_ATTACHMENTS_ROOT,
                agent_name=receiving_agent,
                post_notice=_post_attachment_notice,
                validate_fn=validate_files,
                ingest_fn=ingest_files,
                require_token=True,
            )

    facts = InboundFacts(
        agent_name=receiving_agent,
        conversation_ref=conversation_ref,
        principal_ref=adapter.resolve_principal(user),
        channel_key=channel,
        thread_key=thread_ts,
        text=text,
        author_id=user,
        author_is_bot=author_is_bot,
        mentioned=was_mentioned,
        transport="slack",
        attachments_root=_ATTACHMENTS_ROOT,
        bot_user_map=dict(runtime.bot_user_map),
    )
    await handle_inbound(adapter, facts, ingest=ingest)


async def _handle_event(event: dict, say, client, receiving_agent: str, was_mentioned: bool) -> None:
    """Handle a Slack event for a specific receiving agent.

    Args:
        event: The Slack event dict.
        say: Bolt ``say`` helper, scoped to the receiving agent's app.
        client: Bolt ``client`` (Slack WebClient), scoped to the receiving agent.
        receiving_agent: Name of the agent whose Bolt app received this event.
        was_mentioned: True if the user explicitly @-mentioned this agent in
            this event (i.e. this came in via ``app_mention``).
    """
    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "") or ""
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    event_type = event.get("type", "unknown")

    logger.info(
        "Received event type=%s agent=%s channel=%s user=%s thread_ts=%s text=%s",
        event_type,
        receiving_agent,
        channel,
        user,
        thread_ts,
        text[:80] if text else "",
    )

    # Ignore bot messages to avoid loops, but allow whitelisted dispatch-bot senders through.
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        if _is_dispatch_bot_sender(event, receiving_agent):
            # Guard 1 (issue #547): peer/harness summaries are ingested as context
            # only — they must never create a dispatchable turn. The message remains
            # in Slack and will be visible in thread history on B's next real turn.
            if any(marker in text for marker in SUMMARY_MARKERS):
                logger.info(
                    "guard1: skipping dispatch for peer/harness summary agent=%s text=%.80s",
                    receiving_agent,
                    text,
                )
                return
            logger.info(
                "auto_review: whitelisted dispatch-bot sender=%s bypassing guard, agent=%s",
                event.get("user", ""),
                receiving_agent,
            )
        else:
            logger.debug("Ignoring bot message")
            return

    # Deduplicate by (agent, message identity).  Slack delivers the same user
    # message via both ``app_mention`` and ``message`` — the first arrival
    # processes it; subsequent arrivals within the TTL window are dropped.
    # Scoping the key to ``receiving_agent`` ensures that a multi-mention
    # (@sam @lisa) does not cause one agent's event to shadow the other's.
    _msg_id: str | tuple = event.get("client_msg_id") or (
        channel,
        user,
        event.get("ts", ""),
    )
    _dedup_key: tuple = (receiving_agent, _msg_id)
    if inbound_common.seen_recently(_seen_events, _dedup_key, ttl=_SEEN_EVENTS_TTL, max_size=_SEEN_EVENTS_MAX):
        logger.debug(
            "dedup: dropping duplicate event key=%s agent=%s",
            _dedup_key,
            receiving_agent,
        )
        return

    agent_name = receiving_agent
    agent_map = get_agent_map()
    if agent_name not in agent_map:
        logger.error("Agent %s not found in agent map", agent_name)
        return

    # Record authoritative active agent for this thread BEFORE any short-
    # circuit. Pack commands (grant / revoke / list packs / who has) need
    # this too: the bot's "paste your token" follow-up is a thread reply
    # without a mention, so handle_message routes it only when this agent
    # is recorded as the thread's active agent.
    if channel and thread_ts:
        try:
            get_default_store().set_active_agent(
                channel_id=channel,
                thread_ts=thread_ts,
                agent_name=agent_name,
                mentioned=was_mentioned,
            )
        except Exception:
            logger.exception("Failed to update thread state")

    # #327: Bump the per-thread attachments dir mtime so active threads are
    # preserved by the GC sweep. No-op when the dir doesn't exist yet.
    if thread_ts:
        _bump_attachment_thread_mtime(thread_ts)

    # If a pack's authenticate.py is awaiting the user's next reply in this
    # thread (e.g. "paste your token"), deliver it and stop here — the grant
    # flow will continue on its own.
    if channel and thread_ts and resolve_pending_reply(channel, thread_ts, text):
        return

    # Pack provisioning commands (grant / revoke / list packs / who has) are
    # handled inline by the router rather than dispatched to the agent CLI.
    # Any agent's app can receive them — the target agent is named in the
    # command text. We wrap `say` so every response stays in the same thread
    # as the original command — without that, follow-up replies (e.g. PAT
    # paste) lose their parent and the grant flow can't correlate them.
    async def _threaded_say(reply: str) -> None:
        if thread_ts:
            await say(text=reply, thread_ts=thread_ts)
        else:
            await say(text=reply)

    try:
        if await maybe_handle_pack_command(text, _threaded_say, channel=channel, thread_ts=thread_ts):
            return
    except Exception:
        logger.exception("Error handling pack command (text=%s)", text[:80])
        return

    # #553: flag-gated cutover to the transport-neutral pipeline. Everything
    # above this line is Slack-specific gating shared by both paths (bot
    # guard, dedup, thread-state recording, pack short-circuits); everything
    # below is the legacy body that SLACK_VIA_ADAPTER replaces with
    # chat.core.handle_inbound. Read per event, so the flag flips without a
    # restart and flips back just as fast if dev validation finds a gap.
    if settings.get("SLACK_VIA_ADAPTER"):
        await _handle_event_via_adapter(
            event,
            client,
            receiving_agent=receiving_agent,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            user=user,
            was_mentioned=was_mentioned,
        )
        return

    # Find existing session for this agent+thread or create a new one. When
    # a thread is handed off to a different agent, each agent gets its own
    # session so memory writes and activity timers stay isolated.
    session = find_session_by_thread(
        channel, thread_ts, agent_name=agent_name, timeout_seconds=config.get("session_timeout")
    )
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
        count = 0
        try:
            count = await handle_clean_exit(
                agent_name=agent_name,
                container=agent_config["container"],
                thread_history=session["thread_history"],
                slack_client=client,
                channel=channel,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Error during clean exit for agent %s", agent_name)
        if count > 0:
            await say(text="You're welcome! I've saved our conversation notes.", thread_ts=thread_ts)
        cleanup_session(session["session_id"])
        return

    # Trigger background memory curation if needed (first message of the day).
    # Guard against concurrent curations: skip if one is already in flight for
    # this agent — the in-flight task will write the marker when it finishes.
    agent_config = agent_map[agent_name]
    if needs_curation(agent_name) and not is_curation_in_flight(agent_name):
        logger.info("Triggering background memory curation for %s", agent_name)
        _spawn_background_task(
            curate_agent_memory(agent_name, agent_config["container"]),
            name=f"curate-memory-{agent_name}",
        )

    # Show assistant status indicator while the agent works
    thinking_text = agent_config.get("thinking_status", DEFAULT_THINKING_STATUS)
    await set_assistant_status(client, channel, thread_ts, thinking_text)

    # Record the user's message in session history
    add_to_thread_history(session["session_id"], {"user": user, "text": text})

    # #328: File-attachment ingest — download files and prepend [ATTACHMENTS] block.
    dispatch_text = text
    if attachments_enabled() and thread_ts:
        raw_files = event.get("files") or []
        if raw_files:
            agent_creds = config.get("slack_credentials", {}).get(agent_name, {})
            bot_token = agent_creds.get("bot_token", "")

            async def _post_attachment_notice(notice: str) -> None:
                await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=notice)

            # Shared validate→reject→ingest→warn→abort contract lives in
            # inbound_common; validate/ingest are passed from this module's
            # namespace so existing test patch targets keep intercepting.
            # require_token=True: without a bot token we validate but skip
            # the download (paths stay empty) and the dispatch proceeds.
            paths, ok = await inbound_common.ingest_attachments(
                raw_files,
                thread_ts,
                bot_token,
                attachments_root=_ATTACHMENTS_ROOT,
                agent_name=agent_name,
                post_notice=_post_attachment_notice,
                validate_fn=validate_files,
                ingest_fn=ingest_files,
                require_token=True,
            )
            if not ok:
                return
            block = build_attachments_block(paths)
            if block:
                dispatch_text = block + dispatch_text

    # Dispatch to agent. #422: classify the sender so a genuine human message
    # resets the stuck-guard turn cap. A message that reached here with a
    # bot_id / bot_message subtype is a whitelisted dispatch-bot handoff (see
    # the bot-message guard above), not a human — those must NOT reset the cap.
    human_initiated = not (event.get("bot_id") or event.get("subtype") == "bot_message")
    try:
        result = await dispatch(
            agent_name=agent_name,
            message=dispatch_text,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config.get("session_timeout"),
            bot_user_map=dict(runtime.bot_user_map),
            human_initiated=human_initiated,
        )

        update_activity(session["session_id"])

        response_text = result["response"]
        add_to_thread_history(session["session_id"], {"user": agent_name, "text": response_text})

        if response_text:
            await say(text=md_to_slack(response_text, await outbound_mention_ids(client)), thread_ts=thread_ts)

        logger.info("Responded in thread=%s agent=%s", thread_ts, agent_name)

        # Agent-initiated handoff: if the agent's response @mentions another
        # known agent, promote that agent to "active" so the next message in
        # this thread is dispatched to them (unless the next message mentions
        # someone else, which always wins).
        _maybe_handle_agent_handoff(
            response_text=result["response"],
            current_agent=agent_name,
            channel=channel,
            thread_ts=thread_ts,
        )

    except Exception as exc:
        corr_id = make_correlation_id()
        category, user_msg = build_error_message(exc, corr_id)
        logger.error(
            "Dispatch failure corr_id=%s category=%s agent=%s",
            corr_id,
            category,
            agent_name,
            exc_info=True,
        )
        await say(text=user_msg, thread_ts=thread_ts)


def _maybe_handle_agent_handoff(
    response_text: str,
    current_agent: str,
    channel: str,
    thread_ts: str,
) -> None:
    """If ``response_text`` @mentions another agent, update thread state.

    Agent responses can request handoffs by @-mentioning another agent in
    their reply (e.g. "I'll loop in @dave on this"). When the mentioned
    agent is someone *other* than the current agent, we set them as the
    active agent so the next un-mentioned follow-up goes to them.
    """
    if not channel or not thread_ts or not response_text:
        return

    agent_names = list(get_agent_map().keys())
    mentioned = last_mentioned(response_text, agent_names, runtime.bot_user_map)
    inbound_common.record_handoff(
        mentioned,
        current_agent=current_agent,
        channel_key=channel,
        thread_key=thread_ts,
        store_factory=get_default_store,
    )


async def handle_app_mention(event, say, client, receiving_agent: str) -> None:
    """Handle @mentions of the bot in channels for ``receiving_agent``."""
    await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=True)


def _agent_owns_dispatch_thread(channel: str, thread_ts: str, agent_name: str) -> bool:
    """True when ``agent_name`` has an in-flight dispatch for (channel, thread_ts).

    Used by :func:`handle_message` to route un-mentioned follow-ups in
    dispatch threads to the correct agent even when no ``active_agent``
    row has been written for the thread (e.g. the dispatch was initiated
    before thread-state was established for this channel+ts pair).

    Best-effort: any exception is logged and treated as False so it never
    blocks routing in the normal path.
    """
    try:
        dispatch_id = _dstate.find_dispatch_for_thread(channel, thread_ts)
        if dispatch_id is None:
            return False
        return _dstate.read_field(dispatch_id, _dstate.FIELD_AGENT) == agent_name
    except Exception:
        logger.exception("Failed to check dispatch thread ownership for channel=%s thread=%s", channel, thread_ts)
        return False


async def handle_message(event, say, client, receiving_agent: str) -> None:
    """Handle DMs and thread follow-ups for ``receiving_agent``.

    Dedup rules with multiple per-agent apps installed in a workspace:

    * DMs are scoped to a single bot, so always handle.
    * Channel messages mentioning a *different* agent's bot are deferred
      to that bot's ``app_mention`` handler — skip here.
    * Channel messages mentioning the receiving agent's *own* bot are
      handled here as if ``app_mention`` had fired. Slack suppresses
      ``app_mention`` for self-mentions (a bot @-mentioning itself), so
      without this branch the event would be silently dropped — breaking
      the auto-review handoff where a dispatch worker pings its owning
      agent on completion.
    * Channel thread replies with no mention are handled when this agent
      is the thread's active agent, OR when this agent owns an in-flight
      dispatch for the thread (dispatch threads behave identically to
      direct-mention threads for inbound routing — issue #173).
    """
    channel_type = event.get("channel_type", "")
    text = event.get("text", "") or ""

    # DM to this agent's bot — always handle
    if channel_type == "im":
        await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=False)
        return

    # Channel message that @-mentions a *different* known bot → let the
    # mentioned agent's app_mention handler deal with it.
    own_bot_uid = runtime.bot_user_id_by_agent.get(receiving_agent)
    other_bot_mentioned = any(
        f"<@{uid}>" in text for name, uid in runtime.bot_user_id_by_agent.items() if name != receiving_agent
    )
    if other_bot_mentioned:
        logger.warning(
            "routing.dropped reason=not_mentioned channel=%s agent=%s",
            event.get("channel", ""),
            receiving_agent,
        )
        return

    # Self-mention: Slack does not fire app_mention when a bot mentions
    # itself, so handle it here as if it had. This is the path the auto-
    # review post takes when a dispatch worker pings its owning agent.
    #
    # Gate on sender being a known dispatch bot: when a *human* mentions
    # this bot, Slack already fires ``app_mention`` and that path will
    # dispatch — so handling the duplicate ``message`` event here would
    # double-dispatch (issue #262; same family as #239 / #241 / #245).
    sender_is_known_bot = event.get("user", "") in runtime.dispatch_bot_user_ids
    if sender_is_known_bot and own_bot_uid is not None and f"<@{own_bot_uid}>" in text:
        await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=True)
        return

    # Channel thread reply with no mention — handle iff this agent is active
    # OR this agent owns an in-flight dispatch for the thread.
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event.get("channel", "")
    store_error = False
    active_agent: str | None = None
    try:
        active_agent = get_default_store().get_active_agent(channel, thread_ts)
    except Exception:
        logger.exception("Failed to read thread state")
        store_error = True

    if active_agent != receiving_agent:
        # Dispatch threads route to their owning agent even when active_agent
        # is absent or points to a different agent — the dispatch worker is
        # never interrupted; the reply goes to the agent's normal session.
        if not _agent_owns_dispatch_thread(channel, thread_ts, receiving_agent):
            if store_error:
                reason = "store_error"
            elif active_agent is None:
                reason = "no_active_agent"
            else:
                reason = "not_owned"
            logger.warning(
                "routing.dropped reason=%s channel=%s thread_ts=%s agent=%s",
                reason,
                channel,
                thread_ts,
                receiving_agent,
            )
            return

    await _handle_event(event, say, client, receiving_agent=receiving_agent, was_mentioned=False)
