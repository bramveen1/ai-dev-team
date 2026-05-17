"""Router-side discovery loop — turns launched dispatches into supervised ones.

The dispatch handler runs inside the agent container (Sam) and can only
talk to the shared ``dispatch-workspaces`` volume — not to the router's
SQLite store. So the loop closes that gap from the other side:
periodically scan ``/var/lib/dispatch/`` for dispatch dirs that have a
``pid`` written (i.e. the handler completed its launch) but no matching
supervision task in the store, and call
:func:`router.dispatch.supervision.register_supervision` for each.

This decouples the agent flow from the router DB without forcing the
handler to talk to any router-side API. It also gives router-restart
recovery for free: after a restart, the loop sees every live-pid
dispatch dir and re-registers supervision on the next tick. The
follow-up startup janitor (#159) builds on the same primitive — it just
runs the reconcile once eagerly at boot and additionally garbage-
collects supervision tasks whose dispatch dir is gone.

Skips dispatches that are already terminal (``exitcode`` written),
already halted (``halt_marker`` written), missing ``pid`` (launch not
done), or already supervised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from router.dispatch import state as dstate
from router.dispatch.supervision import CALLABLE_REF, DEFAULT_POLL_PERIOD_SECONDS, register_supervision
from router.scheduled_tasks.store import ScheduledTaskStore

logger = logging.getLogger(__name__)

# Default cadence of the discovery loop. Faster than the supervision
# tick (120s) because this is what closes the window between launch and
# first supervision tick — a 30s discovery cadence keeps that window to
# at most one half-period of the supervisor. Cheap: a directory listing
# and a few file reads per dispatch dir.
DEFAULT_DISCOVERY_INTERVAL_SECONDS = 30

# Resolves an agent's Slack bot user id (the ``U…`` from auth.test) so
# the supervisor's terminal-message ``<@…>`` mention actually pings the
# bot. The router populates this from ``_bot_user_id_by_agent`` in
# :mod:`router.app`. Returning ``None`` is fine — the supervisor falls
# back to the bare agent name, which renders as text only.
AgentUserIdResolver = Callable[[str], str | None]


def _supervised_dispatch_ids(store: ScheduledTaskStore) -> set[str]:
    """Return the dispatch_ids that currently have a supervision task.

    Reads only ``callable_ref = check_dispatch`` rows so we never
    misattribute someone else's system task as "supervision."
    """
    supervised: set[str] = set()
    for task in store.list_by_callable_ref(CALLABLE_REF):
        payload = task.payload or {}
        d_id = payload.get("dispatch_id")
        if isinstance(d_id, str) and d_id:
            supervised.add(d_id)
    return supervised


def _is_launch_complete(dispatch_id: str, *, root: str | None) -> bool:
    """The handler writes ``pid`` last, so ``pid`` present == launch complete.

    See :func:`packs.dispatch.handler.dispatch_issue` for the write
    order — ``started_at``, ``budget``, ``channel``, ``thread_ts``,
    ``agent``, ``issue_url``, ``model``, ``persona`` all land before
    Popen returns, then ``pid``. Treating ``pid`` as the launch sentinel
    means we never register supervision for a half-written dir.
    """
    return dstate.read_field(dispatch_id, dstate.FIELD_PID, root=root) is not None


def reconcile_once(
    store: ScheduledTaskStore,
    *,
    agent_user_id_resolver: AgentUserIdResolver | None = None,
    period_seconds: int = DEFAULT_POLL_PERIOD_SECONDS,
    root: str | None = None,
) -> list[str]:
    """One reconciliation pass. Returns the dispatch_ids freshly registered.

    Idempotent — the supervised-set check means a second call with the
    same input registers nothing. Safe to call from a startup janitor
    *and* the periodic discovery loop without duplicate-task risk.
    """
    supervised = _supervised_dispatch_ids(store)
    registered: list[str] = []

    for dispatch_id in dstate.list_dispatch_ids(root=root):
        if dispatch_id in supervised:
            continue
        if dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=root) is not None:
            continue
        if dstate.read_field(dispatch_id, dstate.FIELD_HALT_MARKER, root=root) is not None:
            # Already on the way out — let any existing supervisor (or
            # the next reconcile after exitcode lands) handle it. We
            # don't want to re-register a halted dispatch just to have
            # the first tick immediately deregister.
            continue
        if not _is_launch_complete(dispatch_id, root=root):
            continue

        agent = dstate.read_field(dispatch_id, dstate.FIELD_AGENT, root=root) or ""
        channel = dstate.read_field(dispatch_id, dstate.FIELD_CHANNEL, root=root) or ""
        thread_ts = dstate.read_field(dispatch_id, dstate.FIELD_THREAD_TS, root=root) or ""
        if not agent:
            logger.warning(
                "Discovery: dispatch dir %s has pid but no agent file; skipping",
                dispatch_id,
            )
            continue

        agent_user_id = agent_user_id_resolver(agent) if agent_user_id_resolver else None
        try:
            register_supervision(
                store,
                dispatch_id=dispatch_id,
                channel=channel,
                thread_ts=thread_ts,
                agent=agent,
                agent_user_id=agent_user_id,
                period_seconds=period_seconds,
            )
        except Exception:
            logger.exception("Discovery: failed to register supervision for %s", dispatch_id)
            continue

        registered.append(dispatch_id)
        logger.info(
            "Discovery: registered supervision for dispatch_id=%s agent=%s (channel=%s thread=%s)",
            dispatch_id,
            agent,
            channel,
            thread_ts,
        )

    return registered


async def run_forever(
    store: ScheduledTaskStore,
    *,
    agent_user_id_resolver: AgentUserIdResolver | None = None,
    interval_seconds: int = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    period_seconds: int = DEFAULT_POLL_PERIOD_SECONDS,
    root: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Discovery main loop. Mirrors the scheduler's run_forever shape.

    ``stop_event`` is honored for graceful shutdown. Exceptions in
    ``reconcile_once`` are logged and swallowed — discovery losing one
    tick to a transient failure must not stop future ticks (same
    invariant the scheduler enforces).
    """
    logger.info("Dispatch discovery loop started (interval=%ds)", interval_seconds)
    while True:
        try:
            reconcile_once(
                store,
                agent_user_id_resolver=agent_user_id_resolver,
                period_seconds=period_seconds,
                root=root,
            )
        except Exception:
            logger.exception("Unhandled error in dispatch discovery reconcile_once")

        if stop_event is not None and stop_event.is_set():
            logger.info("Dispatch discovery loop stopping (stop_event set)")
            return
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                logger.info("Dispatch discovery loop stopping (stop_event set)")
                return
            await asyncio.sleep(interval_seconds)
        except asyncio.TimeoutError:
            continue


def start_discovery_loop(
    store: ScheduledTaskStore,
    *,
    agent_user_id_resolver: AgentUserIdResolver | None = None,
    interval_seconds: int = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    period_seconds: int = DEFAULT_POLL_PERIOD_SECONDS,
    root: str | None = None,
) -> Any:
    """Schedule the discovery loop on the running asyncio event loop.

    Returns the ``asyncio.Task`` so the caller can park a strong
    reference (asyncio drops weak refs and a discarded task can be
    GC'd mid-flight, which is exactly how scheduled tasks silently
    stopped firing in #133's regression).
    """
    return asyncio.create_task(
        run_forever(
            store,
            agent_user_id_resolver=agent_user_id_resolver,
            interval_seconds=interval_seconds,
            period_seconds=period_seconds,
            root=root,
        )
    )
