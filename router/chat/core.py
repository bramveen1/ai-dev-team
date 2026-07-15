"""Transport-agnostic core seam for the ChatAdapter contract.

``run_agent_turn`` is the single entry-point through which any adapter invokes
an agent, and ``handle_inbound`` is the shared inbound-pipeline orchestrator
that wraps it (thread-state recording, sessions, exit trigger, curation,
attachments, dispatch, handoff, classified error replies). Neither contains
transport-specific logic: no Slack client, no channel ID, no thread_ts.
Adapters decode their native events into :class:`InboundFacts` and receive
replies through the ChatAdapter primitives only.

Parity note: this seam mirrors ``router.dispatcher.dispatch`` (the legacy live
Slack path) feature-for-feature — token-budget resolution, session-summary
resume, stuck-guard accounting, pack CLI extras, and API-error classification —
so a transport riding this seam behaves the same as Slack does today.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from router import attachments as attachments_mod
from router.agent_cli import build_cli_command, extract_last_tool_use, parse_cli_result
from router.attachments import build_attachments_block
from router.chat import inbound_common
from router.chat.interface import ChatAdapter
from router.chat.types import AdapterStatus, InboundMessage, OutboundMessage
from router.config import get_agent_map, resolve_default_agent, resolve_session_timeout
from router.container_exec import run_in_container as _run_in_container
from router.context_builder import build_full_context
from router.dispatcher import (
    DEFAULT_MAX_THREAD_MESSAGES,
    DispatchTimeoutError,
    TaskHaltedError,
    _resolve_token_budget,
    _spawn_background_task,
)
from router.error_classifier import build_error_message, make_correlation_id
from router.memory_curator import curate_agent_memory, is_curation_in_flight, needs_curation
from router.memory_loader import load_agent_memory
from router.mentions import last_mentioned
from router.packs.dispatch_hook import pack_cli_extras
from router.session_end import handle_clean_exit, is_exit_trigger
from router.session_manager import (
    add_to_thread_history,
    cleanup_session,
    create_session,
    find_session_by_thread,
    update_activity,
)
from router.stuck_guard import (
    StuckGuard,
    format_slack_message,
    get_default_guard,
    make_task_id,
    write_post_mortem,
)
from router.thread_loader import HARNESS_SUMMARY_EVENT_TYPE, split_messages_at_summary
from router.threads.state import get_default_store

logger = logging.getLogger(__name__)

# Wall-clock budget for one agent CLI turn (30 min). Sized for --max-turns 50
# on Sonnet (~20-25s/turn ≈ 17-21 min — see #200). This is the CLI execution
# budget, deliberately distinct from the SESSION_TIMEOUT idle-session setting;
# it previously aliased session_manager.DEFAULT_TIMEOUT_SECONDS, which happened
# to be 1800 for unrelated reasons. Override per-agent via container_timeout.
AGENT_TURN_TIMEOUT_SECONDS = 1800


async def _notify_conversation(adapter: ChatAdapter, conversation_ref, text: str) -> None:
    """Best-effort notification post; never raises into the caller."""
    try:
        await adapter.send_message(OutboundMessage(text=text, conversation_ref=conversation_ref))
    except Exception:
        logger.exception("Failed to post guard/timeout notification via adapter")


def _record_turn_and_notify(
    *,
    guard: StuckGuard,
    task_id: str,
    agent_name: str,
    adapter: ChatAdapter,
    conversation_ref,
    task_description: str | None,
    tool_name: str | None,
    tool_args: object,
    error_class: str | None,
) -> None:
    """Feed the turn to the guard; on a trip, write the post-mortem and notify.

    Transport-agnostic twin of ``dispatcher._record_and_handle_trip`` — the
    notification goes through ``adapter.send_message`` instead of a Slack
    client, fire-and-forget so the round-trip can't block the next dispatch.
    """
    trip = guard.record_turn(
        task_id=task_id,
        agent_name=agent_name,
        tool_name=tool_name,
        tool_args=tool_args,
        error_class=error_class,
    )
    if trip is None:
        return

    state = guard.get_state(task_id)
    if state is None:
        logger.warning("Guard trip with no state for task=%s; skipping post-mortem", task_id)
        return

    try:
        path = write_post_mortem(
            state=state,
            trip=trip,
            config=guard.config,
            slack_thread_url=None,
            task_description=task_description,
        )
    except Exception:
        logger.exception("Failed to write stuck-guard post-mortem")
        path = None

    text = format_slack_message(state=state, trip=trip, post_mortem_path=path, config=guard.config)
    _spawn_background_task(
        _notify_conversation(adapter, conversation_ref, text),
        name="stuck-guard-notification",
    )


async def run_agent_turn(
    adapter: ChatAdapter,
    inbound: InboundMessage,
    *,
    agent_name: str | None = None,
    timeout: int = AGENT_TURN_TIMEOUT_SECONDS,
    max_token_budget: int | None = None,
    max_thread_messages: int | None = None,
    human_initiated: bool = True,
    guard: StuckGuard | None = None,
) -> str:
    """Route one inbound message through an agent container and return the reply.

    Transport-agnostic: every reference to the transport goes through
    ``adapter``. No Slack client, channel, or thread_ts appear here.

    Resolution order for the agent:
        1. Explicit ``agent_name`` kwarg (caller override).
        2. First ``@mention`` extracted by ``adapter.parse_mentions()``.
        3. ``_DEFAULT_AGENT`` ("sam").

    Args:
        adapter: Any ChatAdapter implementation (terminal, Discord, …).
        inbound: The inbound message to process.
        agent_name: Optional agent override. When ``None``, mentions are parsed
            from ``inbound.text`` and the first one is used.
        timeout: Wall-clock budget in seconds for the CLI invocation.
        max_token_budget: Maximum context tokens. When ``None``, resolves from
            the ``MAX_CONTEXT_TOKENS`` env var and falls back to the default —
            same precedence as the Slack dispatcher.
        max_thread_messages: Maximum thread messages included in context.
            Defaults to the dispatcher's cap (20).
        human_initiated: True when a genuine human message triggered this turn.
            Resets the stuck-guard turn cap and clears a guard-tripped halt for
            the conversation (#422); bot-originated handoffs must pass False.
        guard: StuckGuard override (tests). Defaults to the process singleton.

    Returns:
        The agent's reply text. The same text is also delivered to the
        transport via ``adapter.send_message``.

    Raises:
        ValueError: If the resolved agent name is not in the agent map.
        TaskHaltedError: If the stuck-guard has halted this conversation.
        DispatchError: If the CLI exits non-zero, returns empty output, or
            returns malformed JSON.
    """
    await adapter.set_status(inbound.conversation_ref, AdapterStatus.THINKING)

    # Resolve agent: explicit override > first @mention > configured/discovered default
    if agent_name is None:
        mentions = adapter.parse_mentions(inbound.text, inbound.conversation_ref)
        agent_name = mentions[0] if mentions else resolve_default_agent()

    agent_map = get_agent_map()
    if agent_name not in agent_map:
        raise ValueError(f"Unknown agent: {agent_name}")

    agent_config = agent_map[agent_name]
    container = agent_config["container"]
    display_name = agent_config.get("name", agent_name.capitalize())
    effective_timeout = agent_config.get("container_timeout") or timeout
    effective_budget = _resolve_token_budget(max_token_budget)
    effective_max_messages = max_thread_messages if max_thread_messages is not None else DEFAULT_MAX_THREAD_MESSAGES

    # Stuck-guard pre-check (#422 parity with the Slack dispatcher): a human
    # re-engaging resets the automated turn cap and clears a guard-tripped
    # halt; a halted conversation refuses to dispatch.
    active_guard = guard if guard is not None else get_default_guard()
    task_id = make_task_id(str(inbound.conversation_ref), "", agent_name)
    if human_initiated:
        active_guard.record_human_message(task_id=task_id, agent_name=agent_name)
    if active_guard.is_halted(task_id):
        state = active_guard.get_state(task_id)
        reason = state.halt_reason.description if state and state.halt_reason else "halted"
        logger.warning("Refusing dispatch — task=%s halted by stuck-guard: %s", task_id, reason)
        raise TaskHaltedError(task_id, reason)

    start_time = time.monotonic()

    # Load thread history via adapter (not via Slack API), capped like the
    # Slack path caps conversations.replies output.
    history_messages = await adapter.read_thread(inbound.conversation_ref)
    if len(history_messages) > effective_max_messages:
        history_messages = history_messages[-effective_max_messages:]
    thread_history_dicts = [
        {
            "user": str(msg.principal_ref),
            "text": msg.text,
            "ts": "",
            # Guard 2 (#547): the adapter marks provenance-verified harness
            # summaries; carry the flag so split_messages_at_summary honours
            # them as resume boundaries.
            "metadata": ({"event_type": HARNESS_SUMMARY_EVENT_TYPE} if msg.is_summary else {}),
        }
        for msg in history_messages
    ]

    # Session-summary resume (parity with dispatch()): collapse history at the
    # most recent provenance-verified summary.
    session_summary = None
    context_history = thread_history_dicts
    if thread_history_dicts:
        session_summary, context_history = split_messages_at_summary(thread_history_dicts)
        if session_summary:
            logger.info("Resuming from session summary for agent=%s", agent_name)

    # Build context: memory + history + new message; the inbound text keys
    # retrieval of relevant structured memory when MEMORY_RETRIEVAL_ENABLED
    # is set (#640).
    memory = load_agent_memory(agent_name, query_text=inbound.text)
    context = build_full_context(
        memory=memory,
        thread_history=context_history,
        new_message=inbound.text,
        agent_name=display_name,
        session_summary=session_summary,
        max_tokens=effective_budget,
    )

    # Assemble the Claude CLI invocation (shared with the Slack dispatcher —
    # see router.agent_cli). The conversation_ref flows into the pack extras
    # so the dispatch pack can spawn follow-up dispatches from inside the
    # agent on any transport (TransportRef, #663).
    extras = pack_cli_extras(agent_name, conversation_ref=str(inbound.conversation_ref))
    cli_cmd = build_cli_command(agent_name, agent_config, extras)

    logger.info("run_agent_turn agent=%s container=%s timeout=%ds", agent_name, container, effective_timeout)

    try:
        stdout, stderr, returncode = await _run_in_container(
            container,
            cli_cmd,
            effective_timeout,
            stdin_data=context,
            env=extras.env or None,
        )
    except DispatchTimeoutError:
        _record_turn_and_notify(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            adapter=adapter,
            conversation_ref=inbound.conversation_ref,
            task_description=inbound.text,
            tool_name=None,
            tool_args=None,
            error_class="DispatchTimeoutError",
        )
        last_activity_ts = time.strftime("%H:%M:%S UTC", time.gmtime())
        timeout_text = (
            f":alarm_clock: Agent *{display_name}* hit the router timeout ({effective_timeout}s)"
            f" — last activity at {last_activity_ts}."
            f" Likely needs the timeout raised or the task split."
        )
        await _notify_conversation(adapter, inbound.conversation_ref, timeout_text)
        raise

    duration = time.monotonic() - start_time

    def _record_error(error_class: str) -> None:
        _record_turn_and_notify(
            guard=active_guard,
            task_id=task_id,
            agent_name=agent_name,
            adapter=adapter,
            conversation_ref=inbound.conversation_ref,
            task_description=inbound.text,
            tool_name=None,
            tool_args=None,
            error_class=error_class,
        )

    data, response_text = parse_cli_result(
        agent_name=agent_name,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        record_error=_record_error,
    )

    # Successful turn — record it for guard accounting, same as dispatch().
    last_tool_name, last_tool_args = extract_last_tool_use(data)
    _record_turn_and_notify(
        guard=active_guard,
        task_id=task_id,
        agent_name=agent_name,
        adapter=adapter,
        conversation_ref=inbound.conversation_ref,
        task_description=inbound.text,
        tool_name=last_tool_name,
        tool_args=last_tool_args,
        error_class=None,
    )

    await adapter.send_message(OutboundMessage(text=response_text, conversation_ref=inbound.conversation_ref))
    await adapter.set_status(inbound.conversation_ref, AdapterStatus.DONE)

    logger.info("run_agent_turn agent=%s response_len=%d duration=%.2fs", agent_name, len(response_text), duration)
    return response_text


# ---------------------------------------------------------------------------
# Shared inbound-pipeline orchestrator (roadmap §2c slice 2)
# ---------------------------------------------------------------------------


@dataclass
class InboundFacts:
    """Transport-decoded facts about one inbound message.

    Adapters translate their native event (Slack event dict, Discord gateway
    message) into this shape *after* running their transport-specific gates
    (self-message skip, mention/thread-follow routing, dedup, bot allowlist,
    summary guard) and hand it to :func:`handle_inbound`.

    ``channel_key``/``thread_key`` are the transport's stable string keys for
    thread-state and session bookkeeping (Slack: channel id + thread_ts;
    Discord: channel id + thread id, falling back to the message id).
    ``bot_user_map`` feeds handoff mention-resolution on transports that
    address agents by user-id token (Slack); ``None`` matches by name.
    """

    agent_name: str
    conversation_ref: Any
    principal_ref: Any
    channel_key: str
    thread_key: str
    text: str
    author_id: str
    author_is_bot: bool
    mentioned: bool
    transport: str
    attachments_root: str = attachments_mod._ATTACHMENTS_ROOT
    bot_user_map: dict[str, str] | None = field(default=None)


async def handle_inbound(
    adapter: ChatAdapter,
    facts: InboundFacts,
    *,
    ingest: Callable[[], Awaitable[tuple[list[str], bool]]] | None = None,
    guard: StuckGuard | None = None,
) -> None:
    """Run the transport-neutral inbound pipeline for one decoded message.

    Stage order is load-bearing and mirrors the legacy Slack path
    (``slack_events._handle_event``) stage for stage:

    1. Record the authoritative active agent for the thread.
    2. Refresh the attachments GC TTL.
    3. Find or create the session (tagging transport + conversation_ref).
    4. Clean-exit trigger → persist memory, thank the user, close session.
    5. Kick background memory curation on the first message of the day.
    6. Append the user message to session history.
    7. Ingest attachments via the adapter's *ingest* callback (None = skip);
       an ingest abort ends the turn — a user-facing notice was already
       posted by the callback.
    8. Dispatch through :func:`run_agent_turn`; record history + handoff.
    9. On dispatch failure, post the classified error reply.
    """
    # Stage 1: record the active agent BEFORE any short-circuit so follow-up
    # routing sees this thread as owned.
    try:
        get_default_store().set_active_agent(
            channel_id=facts.channel_key,
            thread_ts=facts.thread_key,
            agent_name=facts.agent_name,
            mentioned=facts.mentioned,
        )
    except Exception:
        logger.exception("Failed to update thread state")

    # Stage 2 (#327): keep the attachments GC TTL fresh for active threads.
    inbound_common.bump_attachment_thread_mtime(facts.thread_key, attachments_root=facts.attachments_root)

    # Stage 3: one session per agent+thread, powering timeout summaries and
    # clean-exit memory.
    session = find_session_by_thread(
        facts.channel_key,
        facts.thread_key,
        agent_name=facts.agent_name,
        timeout_seconds=resolve_session_timeout(),
    )
    if session is None:
        session = create_session(channel=facts.channel_key, thread_ts=facts.thread_key, agent_name=facts.agent_name)
        session["transport"] = facts.transport
        session["conversation_ref"] = str(facts.conversation_ref)
    else:
        update_activity(session["session_id"])

    agent_config = get_agent_map().get(facts.agent_name)

    # Stage 4: clean-exit trigger ("thanks", "bye", …) — persist memory, close.
    if is_exit_trigger(facts.text):
        logger.info("Exit trigger detected in thread=%s from user=%s", facts.thread_key, facts.author_id)
        count = 0
        if agent_config is not None:
            try:
                count = await handle_clean_exit(
                    agent_name=facts.agent_name,
                    container=agent_config["container"],
                    thread_history=session["thread_history"],
                    slack_client=None,
                    channel=facts.channel_key,
                    thread_ts=facts.thread_key,
                )
            except Exception:
                logger.exception("Error during clean exit for agent %s", facts.agent_name)
        if count > 0:
            await adapter.send_message(
                OutboundMessage(
                    text="You're welcome! I've saved our conversation notes.",
                    conversation_ref=facts.conversation_ref,
                )
            )
        cleanup_session(session["session_id"])
        return

    # Stage 5: background memory curation on the first message of the day.
    if agent_config is not None and needs_curation(facts.agent_name) and not is_curation_in_flight(facts.agent_name):
        logger.info("Triggering background memory curation for %s", facts.agent_name)
        _spawn_background_task(
            curate_agent_memory(facts.agent_name, agent_config["container"]),
            name=f"curate-memory-{facts.agent_name}",
        )

    # Stage 6: record the user's message in session history.
    add_to_thread_history(session["session_id"], {"user": facts.author_id, "text": facts.text})

    # Stage 7 (#328): attachment ingest — the adapter's callback downloads and
    # posts any user-facing rejection/failure notices; an abort ends the turn.
    dispatch_text = facts.text
    attachment_paths: list[str] = []
    if ingest is not None:
        attachment_paths, ok = await ingest()
        if not ok:
            return
        block = build_attachments_block(attachment_paths)
        if block:
            dispatch_text = block + dispatch_text

    inbound = InboundMessage(
        conversation_ref=facts.conversation_ref,
        principal_ref=facts.principal_ref,
        text=dispatch_text,
        attachments=attachment_paths,
    )

    # Stages 8-9: dispatch, then history + handoff on success or a classified
    # error reply on failure.
    try:
        response_text = await run_agent_turn(
            adapter,
            inbound,
            agent_name=facts.agent_name,
            human_initiated=not facts.author_is_bot,
            guard=guard,
        )
        update_activity(session["session_id"])
        add_to_thread_history(session["session_id"], {"user": facts.agent_name, "text": response_text})
        try:
            agent_names = list(get_agent_map().keys())
            mentioned = last_mentioned(response_text, agent_names, facts.bot_user_map)
        except Exception:
            logger.exception("Failed to record agent-initiated handoff")
            mentioned = None
        inbound_common.record_handoff(
            mentioned,
            current_agent=facts.agent_name,
            channel_key=facts.channel_key,
            thread_key=facts.thread_key,
            store_factory=get_default_store,
        )
    except Exception as exc:
        corr_id = make_correlation_id()
        category, user_msg = build_error_message(exc, corr_id)
        logger.error(
            "Dispatch failure corr_id=%s category=%s agent=%s",
            corr_id,
            category,
            facts.agent_name,
            exc_info=True,
        )
        try:
            await adapter.set_status(facts.conversation_ref, AdapterStatus.ERROR)
            await adapter.send_message(OutboundMessage(text=user_msg, conversation_ref=facts.conversation_ref))
        except Exception:
            logger.error("handle_inbound[%s]: failed to post error notification", facts.agent_name, exc_info=True)
