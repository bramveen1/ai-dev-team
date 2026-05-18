"""Router-side polling supervision for in-flight dispatches (#163).

The scheduled-tasks scheduler invokes :func:`check_dispatch` every
``period_seconds`` (default 120s) per active dispatch. Each tick reads
the sidecar state files under ``/var/lib/dispatch/<dispatch_id>/``,
posts at most one Slack line if something changed or terminated, and
returns ``{"status": "done"}`` on terminal so the scheduler deregisters
the system task.

Detection priority — checked in this order, first match wins:

1. **Terminal** — ``exitcode`` file present. Post a summary; deregister.
2. **Halt marker** — operator-driven ``/kill``. SIGTERM the pid, write
   a synthetic ``exitcode=-1``, post ``killed``; deregister.
3. **Budget exceeded** — ``now - started_at > budget``. SIGTERM the pid,
   write synthetic ``exitcode=-1``, post ``timeout``; deregister.
4. **Orphan** — pid is no longer alive but no ``exitcode`` was written.
   Synthetic ``exitcode=-1``; post ``orphan``; deregister.
5. **Delta** — ``last_event``/``last_tool``/``cost`` changed since the
   last tick. Post one delta line. Keep polling.
6. **Quiet** — nothing changed; emit nothing.

A small in-process dict (:data:`_last_posted`) holds the last delta we
posted per dispatch so successive ticks compare-and-skip. It's
process-local, so a router restart loses delta state — that's fine,
the worst case is one redundant delta line after recovery.

The supervisor does *not* exec into the agent container — it reads files
off the shared ``dispatch-workspaces`` volume (which router/compose now
mounts r/w into the router service) and posts via the agent owner's
Slack bot client (resolved by the scheduler's ``client_resolver``).
"""

from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timezone
from typing import Any

from router.dispatch import state as dstate

logger = logging.getLogger(__name__)

# Fields whose change triggers a per-tick delta line. Listed explicitly
# so adding informational fields to the state dir (e.g. ``persona``)
# doesn't accidentally start spamming Slack.
DELTA_FIELDS = (dstate.FIELD_LAST_EVENT, dstate.FIELD_LAST_TOOL, dstate.FIELD_COST)

# Cache of the last delta dict posted per dispatch_id. Used to suppress
# "nothing actually changed" ticks. Module-level so successive scheduler
# ticks see each other's writes; this is safe because the scheduler runs
# the supervisor on a single asyncio loop.
_last_posted: dict[str, dict[str, str]] = {}


def reset_delta_cache() -> None:
    """Test hook — wipe the in-process delta cache."""
    _last_posted.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _format_agent_mention(agent: str, agent_user_id: str | None = None) -> str:
    """Return the agent's @-mention token.

    Slack only resolves ``<@USER_ID>`` (the ``U…`` user id from
    ``auth.test``) into an actual ping that wakes the bot's app_mention
    handler. ``<@sam>`` is just visible text Slack does not route. So
    when ``agent_user_id`` is available (the discovery loop resolves it
    from the agent map at registration time), prefer that. Fall back to
    the bare agent name so legacy payloads still render something
    human-readable even if they won't trigger the bot.
    """
    target = agent_user_id or agent
    if not target:
        return ""
    return f"<@{target}>"


def _terminal_summary(
    dispatch_id: str,
    agent: str,
    exitcode: int,
    state: dict[str, str],
    started_at: datetime | None,
    now: datetime,
    agent_user_id: str | None = None,
) -> str:
    """Render the one-line terminal message posted to Slack on dispatch end."""
    if exitcode == 0:
        head = f":white_check_mark: dispatch `{dispatch_id}` done (exit 0)"
    elif exitcode == -1:
        head = f":warning: dispatch `{dispatch_id}` terminated (exit -1)"
    else:
        head = f":x: dispatch `{dispatch_id}` failed (exit {exitcode})"

    parts = [head]
    if started_at is not None:
        parts.append(f"duration: {_format_duration((now - started_at).total_seconds())}")
    cost = state.get(dstate.FIELD_COST)
    if cost:
        parts.append(f"cost: ${cost}")
    pr_url = state.get(dstate.FIELD_PR_URL)
    if pr_url:
        parts.append(f"PR: {pr_url}")
    mention = _format_agent_mention(agent, agent_user_id)
    if mention:
        parts.append(mention)
    return " · ".join(parts)


def _delta_line(dispatch_id: str, prev: dict[str, str], curr: dict[str, str]) -> str | None:
    """Return one delta line if any tracked field changed; else ``None``."""
    bits: list[str] = []
    for field_name, label in (
        (dstate.FIELD_LAST_EVENT, "event"),
        (dstate.FIELD_LAST_TOOL, "tool"),
        (dstate.FIELD_COST, "cost"),
    ):
        new_value = curr.get(field_name)
        if new_value and new_value != prev.get(field_name):
            prefix = "$" if field_name == dstate.FIELD_COST else ""
            bits.append(f"{label}: {prefix}{new_value}")
    if not bits:
        return None
    return f"`{dispatch_id}` · " + " · ".join(bits)


async def _post(slack_client: Any, channel: str, thread_ts: str, text: str) -> None:
    """Best-effort thread post. Never raises (supervisor must keep polling)."""
    if slack_client is None or not channel:
        return
    try:
        await slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts or None, text=text)
    except Exception:
        logger.exception("Supervision: failed to post to channel=%s thread=%s", channel, thread_ts)


def _send_sigterm(pid_str: str | None) -> bool:
    """SIGTERM the subprocess (and its process group). Returns True on success.

    The handler spawns the babysit with ``start_new_session=True``, which
    makes the babysit's pid the leader of a new process group (pgid =
    pid). ``os.killpg(pid, SIGTERM)`` therefore signals the babysit *and*
    every claude child it has open in one call — the right semantics for
    "tear this whole dispatch down." We fall back to ``os.kill(pid, …)``
    when ``killpg`` raises PermissionError (test fixtures that didn't
    detach the babysit into its own pgid) so unit tests can still
    exercise this path.
    """
    if not pid_str:
        return False
    try:
        pid = int(pid_str)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    for killer in (lambda p: os.killpg(p, signal.SIGTERM), lambda p: os.kill(p, signal.SIGTERM)):
        try:
            killer(pid)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            continue
    return False


def _write_synthetic_exitcode_if_absent(
    dispatch_id: str,
    *,
    dispatch_root: str | None,
    value: str = "-1",
) -> str:
    """Write ``exitcode=value`` only if no real exitcode was already written.

    Closes the tiny race window between the supervisor deciding to kill
    a dispatch (halt_marker / timeout / orphan path) and the babysit
    writing a real exit code from the child it was already shutting
    down. Re-reads exitcode after SIGTERM; returns the actual recorded
    value so the caller can render an accurate terminal summary instead
    of unconditionally claiming exit -1.
    """
    existing = dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root)
    if existing is not None:
        return existing
    dstate.write_field(dispatch_id, dstate.FIELD_EXITCODE, value, root=dispatch_root)
    return value


async def check_dispatch(
    *,
    payload: dict,
    slack_client: Any,
    now: datetime | None = None,
    dispatch_root: str | None = None,
) -> dict:
    """One supervision tick. Called by the scheduler every ``period_seconds``.

    Returns ``{"status": "done", "reason": <str>}`` on terminal so the
    scheduler deletes the system task. Returns ``{"status": "ok"}`` to
    stay scheduled.
    """
    dispatch_id = payload["dispatch_id"]
    channel = payload.get("channel", "")
    thread_ts = payload.get("thread_ts", "")
    agent = payload.get("agent", "")
    agent_user_id = payload.get("agent_user_id") or None
    now = now or _now()

    # Workspace was wiped (e.g. by dispatch_cancel) — deregister cleanly
    # without posting anything; the cancel confirmation went to Slack via
    # the agent's tool-call response already.
    if not dstate.dispatch_dir(dispatch_id, root=dispatch_root).is_dir():
        _last_posted.pop(dispatch_id, None)
        return {"status": "done", "reason": "workspace_gone"}

    state = dstate.read_state(dispatch_id, root=dispatch_root)
    started_at = _parse_iso(state.get(dstate.FIELD_STARTED_AT))
    pid = state.get(dstate.FIELD_PID)

    # 1. Terminal — exitcode was written. Could be a normal subprocess
    # exit (babysit), a synthetic from a previous orphan/timeout pass
    # (us), or a manual write from the smoke probe.
    exitcode_raw = state.get(dstate.FIELD_EXITCODE)
    if exitcode_raw is not None:
        try:
            exitcode = int(exitcode_raw)
        except ValueError:
            exitcode = -1
        text = _terminal_summary(dispatch_id, agent, exitcode, state, started_at, now, agent_user_id)
        await _post(slack_client, channel, thread_ts, text)
        _last_posted.pop(dispatch_id, None)
        return {"status": "done", "reason": "exitcode", "exitcode": exitcode}

    # 2. Halt marker — /kill matched this dispatch. SIGTERM, then write a
    # synthetic exitcode ONLY if the babysit didn't already write a real
    # one between our state read and now (closes the corruption window
    # the reviewer called out: SIGTERM during a successful exit can race
    # the babysit's normal exitcode=0 write).
    if state.get(dstate.FIELD_HALT_MARKER) is not None:
        _send_sigterm(pid)
        _write_synthetic_exitcode_if_absent(dispatch_id, dispatch_root=dispatch_root)
        dstate.write_field(dispatch_id, dstate.FIELD_CANCEL_REASON, "stuck_guard_kill", root=dispatch_root)
        text_parts = [f":octagonal_sign: dispatch `{dispatch_id}` killed (operator halt)"]
        mention = _format_agent_mention(agent, agent_user_id)
        if mention:
            text_parts.append(mention)
        await _post(slack_client, channel, thread_ts, " · ".join(text_parts))
        _last_posted.pop(dispatch_id, None)
        return {"status": "done", "reason": "killed"}

    # 3. Budget exceeded — handler wrote `budget` (seconds), supervisor
    # compares elapsed vs budget on every tick. SIGTERM + synthetic
    # exitcode mirrors the halt path; the message is different so the
    # operator can tell timeout from manual kill.
    budget_raw = state.get(dstate.FIELD_BUDGET)
    if started_at is not None and budget_raw:
        try:
            budget = int(budget_raw)
        except ValueError:
            budget = 0
        if budget > 0 and (now - started_at).total_seconds() > budget:
            _send_sigterm(pid)
            _write_synthetic_exitcode_if_absent(dispatch_id, dispatch_root=dispatch_root)
            dstate.write_field(dispatch_id, dstate.FIELD_CANCEL_REASON, "runtime_timeout", root=dispatch_root)
            text_parts = [f":alarm_clock: dispatch `{dispatch_id}` timed out after {_format_duration(budget)}"]
            mention = _format_agent_mention(agent, agent_user_id)
            if mention:
                text_parts.append(mention)
            await _post(slack_client, channel, thread_ts, " · ".join(text_parts))
            _last_posted.pop(dispatch_id, None)
            return {"status": "done", "reason": "timeout"}

    # 4. Orphan — pid is gone but no exitcode written. This is the
    # "babysit died, subprocess died" failure mode. Synthesize exitcode
    # so downstream consumers see terminal state, and post a clear
    # orphan signal so the operator knows to investigate.
    if pid is not None:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = -1
        if pid_int > 0 and not dstate.pid_alive(pid_int):
            _write_synthetic_exitcode_if_absent(dispatch_id, dispatch_root=dispatch_root)
            text_parts = [f":ghost: dispatch `{dispatch_id}` orphaned (no exitcode, pid gone)"]
            mention = _format_agent_mention(agent, agent_user_id)
            if mention:
                text_parts.append(mention)
            await _post(slack_client, channel, thread_ts, " · ".join(text_parts))
            _last_posted.pop(dispatch_id, None)
            return {"status": "done", "reason": "orphan"}

    # 5. Delta — interesting fields changed since last tick. Post one
    # line and update the cache so the next tick only fires when there's
    # something new.
    interesting = {k: state.get(k, "") for k in DELTA_FIELDS}
    line = _delta_line(dispatch_id, _last_posted.get(dispatch_id, {}), interesting)
    if line:
        await _post(slack_client, channel, thread_ts, line)
        _last_posted[dispatch_id] = interesting

    return {"status": "ok"}


CALLABLE_REF = "router.dispatch.supervision:check_dispatch"
DEFAULT_POLL_PERIOD_SECONDS = 120


def register_supervision(
    store,
    *,
    dispatch_id: str,
    channel: str,
    thread_ts: str,
    agent: str,
    agent_user_id: str | None = None,
    period_seconds: int = DEFAULT_POLL_PERIOD_SECONDS,
) -> Any:
    """Register the supervision system task for a freshly launched dispatch.

    Called from :func:`router.dispatch.discovery.reconcile_once` once it
    notices a launched-but-unsupervised dispatch dir. Wraps the
    :meth:`ScheduledTaskStore.create_system_task` boilerplate so callers
    don't need to know the callable_ref or payload schema.

    ``agent_user_id`` is the Slack user id from the agent's
    ``auth.test`` response — required if the terminal message's
    ``<@…>`` mention is to actually ping the bot. When omitted the
    supervisor falls back to ``<@<agent_name>>`` which renders as text
    only.

    Validates ``dispatch_id`` at register time so a malformed payload
    can't sit in the scheduler forever raising the same KeyError on
    every tick.
    """
    if not dispatch_id:
        raise ValueError("register_supervision requires a non-empty dispatch_id")
    if not agent:
        raise ValueError("register_supervision requires a non-empty agent name")

    payload: dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "agent": agent,
    }
    if agent_user_id:
        payload["agent_user_id"] = agent_user_id

    # No ``destination=`` on the system task — the supervisor reads
    # everything it needs from ``payload`` and the scheduler's
    # ``resolve_destination`` only applies to agent (cron) tasks. Passing
    # one here would be misleading dead weight in the SQLite row.
    return store.create_system_task(
        agent_name=agent,
        name=f"supervise {dispatch_id}",
        callable_ref=CALLABLE_REF,
        payload=payload,
        period_seconds=period_seconds,
    )


def mark_halted_for_agent(agent_name: str, *, root: str | None = None) -> list[str]:
    """Write a halt_marker into every in-flight dispatch owned by ``agent_name``.

    Used by :mod:`router.kill_command` to translate a ``/kill <agent>``
    into the file-based halt contract the supervisor watches for. Skips
    dispatches that are already terminal (``exitcode`` written) or
    already halted (``halt_marker`` written). Returns the list of
    ``dispatch_id``s that were freshly marked so the kill command can
    include the count in its ack.
    """
    halted: list[str] = []
    for dispatch_id in dstate.list_dispatch_ids(root=root):
        owner = dstate.read_field(dispatch_id, dstate.FIELD_AGENT, root=root)
        if owner != agent_name:
            continue
        if dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=root) is not None:
            continue
        if dstate.read_field(dispatch_id, dstate.FIELD_HALT_MARKER, root=root) is not None:
            continue
        try:
            dstate.write_field(
                dispatch_id,
                dstate.FIELD_HALT_MARKER,
                _now().isoformat(),
                root=root,
            )
            halted.append(dispatch_id)
        except OSError:
            logger.exception("Failed to write halt_marker for dispatch=%s", dispatch_id)
    return halted
