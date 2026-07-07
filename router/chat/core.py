"""Transport-agnostic core seam for the ChatAdapter contract.

``run_agent_turn`` is the single entry-point through which any adapter invokes
an agent. It contains no transport-specific logic: no Slack client, no channel
ID, no thread_ts. Adapters supply thread history and receive the reply through
the ChatAdapter primitives only.

Parity note: this seam mirrors ``router.dispatcher.dispatch`` (the legacy live
Slack path) feature-for-feature — token-budget resolution, session-summary
resume, stuck-guard accounting, pack CLI extras, and API-error classification —
so a transport riding this seam behaves the same as Slack does today.
"""

from __future__ import annotations

import logging
import time

from router.agent_cli import build_cli_command, extract_last_tool_use, parse_cli_result
from router.chat.interface import ChatAdapter
from router.chat.types import AdapterStatus, InboundMessage, OutboundMessage
from router.config import get_agent_map, resolve_default_agent
from router.container_exec import run_in_container as _run_in_container
from router.context_builder import build_full_context
from router.dispatcher import (
    DEFAULT_MAX_THREAD_MESSAGES,
    DispatchTimeoutError,
    TaskHaltedError,
    _resolve_token_budget,
    _spawn_background_task,
)
from router.memory_loader import load_agent_memory
from router.packs.dispatch_hook import pack_cli_extras
from router.stuck_guard import (
    StuckGuard,
    format_slack_message,
    get_default_guard,
    make_task_id,
    write_post_mortem,
)
from router.thread_loader import HARNESS_SUMMARY_EVENT_TYPE, split_messages_at_summary

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
