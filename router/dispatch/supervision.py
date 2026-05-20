"""Router-side polling supervision for in-flight dispatches (#163).

The scheduled-tasks scheduler invokes :func:`check_dispatch` every
``period_seconds`` (default 120s) per active dispatch. Each tick reads
the sidecar state files under ``/var/lib/dispatch/<dispatch_id>/``,
posts at most one Slack line if something changed or terminated, and
returns ``{"status": "done"}`` on terminal so the scheduler deregisters
the system task.

Detection priority — checked in this order, first match wins:

1. **Terminal** — ``exitcode`` file present. Post a summary; deregister.
2. **Halt marker** — operator-driven ``/kill``. Wait up to 60 s for
   babysit to detect the marker and write its own exitcode, then write a
   synthetic ``exitcode=-1`` if it doesn't respond; post ``killed``;
   deregister.
3. **Budget exceeded** — ``now - started_at > budget``. Write a
   ``timeout_marker`` so babysit self-terminates; wait up to 60 s for
   its exitcode; write synthetic ``exitcode=-1`` as fallback; post
   ``timeout``; deregister.
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

import asyncio
import logging

# D-5: Quota module lives in the pack dir, which is mounted at /app/packs/
# in the router container (see docker-compose.yml). We import it dynamically
# so the router doesn't hard-depend on the pack dir being importable.
import sys as _sys
import time
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Any

from router.dispatch import state as dstate

_QUOTA_PACK_DIR = _Path(__file__).resolve().parent.parent.parent / "packs" / "dispatch"
if _QUOTA_PACK_DIR.is_dir() and str(_QUOTA_PACK_DIR) not in _sys.path:
    _sys.path.insert(0, str(_QUOTA_PACK_DIR))

try:
    import quota as _quota_mod

    _QUOTA_AVAILABLE = True
except ImportError:
    _quota_mod = None  # type: ignore[assignment]
    _QUOTA_AVAILABLE = False

# D-206: Slot-release helper lives in the pack's handler.py.  Load it
# via importlib so we don't cache it as the bare name "handler" in
# sys.modules (which would conflict with other packs' handler.py files
# that share the same short name).  Best-effort: a missing or partially-
# upgraded pack dir must never kill the supervision loop.
_release_slot_for_dispatch = None  # type: ignore[assignment]
_POOL_SLOTS_DIR_NAME = ".slots"
_SLOT_RELEASE_AVAILABLE = False
try:
    import importlib.util as _importlib_util

    _hspec = _importlib_util.spec_from_file_location(
        "_dispatch_pack_handler",
        _QUOTA_PACK_DIR / "handler.py",
    )
    if _hspec is not None and _hspec.loader is not None:
        _dispatch_handler_mod = _importlib_util.module_from_spec(_hspec)
        _hspec.loader.exec_module(_dispatch_handler_mod)
        _release_slot_for_dispatch = _dispatch_handler_mod._release_slot_for_dispatch
        _POOL_SLOTS_DIR_NAME = _dispatch_handler_mod.POOL_SLOTS_DIR_NAME
        _SLOT_RELEASE_AVAILABLE = True
except Exception:
    pass

# Config path: /config/dispatch.yaml (the mounted config volume) in production,
# or the pack-dir-relative path for dev/test (falls back to safe defaults).
_QUOTA_CONFIG_PATH = _Path("/config/dispatch.yaml")


def _load_quota_config() -> dict:
    if not _QUOTA_AVAILABLE or _quota_mod is None:
        return {"threshold_usd": 50.0, "window_hours": 5.0}
    if _QUOTA_CONFIG_PATH.exists():
        return _quota_mod.load_config(_QUOTA_CONFIG_PATH)
    # Dev/test fallback — quota.load_config handles missing file gracefully.
    alt = _QUOTA_PACK_DIR.parent.parent / "config" / "dispatch.yaml"
    return _quota_mod.load_config(alt)


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


# How long to wait for the babysit to self-terminate after a marker is
# written before falling back to a synthetic exitcode. Tests can monkeypatch
# these to 0 / a tiny value for fast execution.
_TERMINATION_WAIT_SECONDS: float = 60.0
_TERMINATION_POLL_INTERVAL: float = 5.0


async def _wait_for_exitcode(
    dispatch_id: str,
    *,
    dispatch_root: str | None,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> str | None:
    """Poll for the exitcode file for up to *timeout* seconds.

    Returns the exitcode string as soon as babysit writes it, or ``None``
    if the timeout elapses without a file appearing. Called after the
    supervisor writes a halt/timeout marker so we can confirm termination
    before posting the Slack message.
    """
    t = timeout if timeout is not None else _TERMINATION_WAIT_SECONDS
    p = poll_interval if poll_interval is not None else _TERMINATION_POLL_INTERVAL
    deadline = time.monotonic() + t
    while True:
        val = dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root)
        if val is not None:
            return val
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(p, remaining))


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


async def _fire_quota_hooks(
    root: _Path,
    now: "datetime",
    slack_client: Any,
    channel: str,
    thread_ts: str,
) -> None:
    """Fire quota logging and warning hooks after a dispatch reaches terminal state.

    Called from the terminal-state branch of :func:`check_dispatch` so
    quota accounting fires exactly once per dispatch in both inline and
    poll supervision modes. Best-effort — never raises.
    """
    if not _QUOTA_AVAILABLE or _quota_mod is None:
        return
    cfg = _load_quota_config()
    threshold_usd = cfg["threshold_usd"]
    window_hours = cfg["window_hours"]

    try:
        _quota_mod.log_window_oneliner(root, now, logger.info, window_hours=window_hours)
    except Exception:
        logger.exception("supervision: quota.log_window_oneliner failed")

    # Warning: compute inline so we can post with the async _post helper.
    try:
        window_cost, _, _ = _quota_mod.window_state(root, now, window_hours=window_hours)
        if window_cost >= 0.8 * threshold_usd:
            sentinel = root / f"{_quota_mod.WARNING_SENT_PREFIX}{_quota_mod.window_start_unix(now, window_hours)}"
            if not sentinel.exists():
                warn_text = (
                    f":warning: Quota heads-up: ${window_cost:.2f} spent this window "
                    f"({window_cost / threshold_usd * 100:.0f}% of ${threshold_usd:.0f} limit). "
                    f"Soft-lock engages at 100%."
                )
                await _post(slack_client, channel, thread_ts, warn_text)
                try:
                    sentinel.touch()
                except OSError:
                    pass
    except Exception:
        logger.exception("supervision: quota warning check failed")


def format_auto_review_text(mention: str, dispatch_id: str, pr_url: str) -> str:
    """Return the auto-review @-mention text posted by ``_maybe_fire_auto_review``.

    Exported so ``router.app._is_auto_review_mention`` and tests can derive the
    expected string from this single source of truth instead of hand-crafting it.
    """
    return f"{mention} dispatch `{dispatch_id}` completed, PR ready for review: {pr_url}"


async def _maybe_fire_auto_review(
    dispatch_id: str,
    *,
    pr_url: str,
    agent: str,
    agent_user_id: str | None,
    slack_client: Any,
    channel: str,
    thread_ts: str,
    dispatch_root: str | None,
) -> None:
    """Post a review @-mention once for a successful dispatch with a PR.

    Writes ``FIELD_AUTO_REVIEW_FIRED`` as an idempotency marker so a
    router restart cannot fire the mention a second time.  The message
    format ``<@AGENT> dispatch <id> completed, PR ready for review: <url>``
    lands in the dispatch thread, where the router's app_mention handler
    (issue #173) re-enters the originating agent's session.
    """
    marker_path = dstate.dispatch_dir(dispatch_id, root=dispatch_root) / dstate.FIELD_AUTO_REVIEW_FIRED
    if marker_path.exists():
        return
    mention = _format_agent_mention(agent, agent_user_id)
    text = format_auto_review_text(mention, dispatch_id, pr_url)
    await _post(slack_client, channel, thread_ts, text)
    try:
        marker_path.touch()
    except OSError:
        logger.warning("auto_review: failed to write marker for dispatch=%s", dispatch_id)


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

        # D-5: Fire quota hooks once per terminal dispatch.
        await _fire_quota_hooks(
            dstate.dispatch_root(dispatch_root),
            now,
            slack_client,
            channel,
            thread_ts,
        )

        # Issue #207: Auto-invoke PR review on successful completion.
        if exitcode == 0:
            pr_url = state.get(dstate.FIELD_PR_URL)
            if pr_url:
                await _maybe_fire_auto_review(
                    dispatch_id,
                    pr_url=pr_url,
                    agent=agent,
                    agent_user_id=agent_user_id,
                    slack_client=slack_client,
                    channel=channel,
                    thread_ts=thread_ts,
                    dispatch_root=dispatch_root,
                )

        return {"status": "done", "reason": "exitcode", "exitcode": exitcode}

    # 2. Halt marker — /kill matched this dispatch. The marker file is the
    # signal channel; babysit polls it and self-terminates (namespace-safe,
    # no cross-container os.killpg). We wait up to 60 s for babysit to
    # write its own exitcode, then fall back to a synthetic one.
    if state.get(dstate.FIELD_HALT_MARKER) is not None:
        dstate.write_field(dispatch_id, dstate.FIELD_CANCEL_REASON, "stuck_guard_kill", root=dispatch_root)
        await _wait_for_exitcode(dispatch_id, dispatch_root=dispatch_root)
        _write_synthetic_exitcode_if_absent(dispatch_id, dispatch_root=dispatch_root)
        # Release slot after state writes (preserve state-before-cleanup invariant).
        try:
            if _SLOT_RELEASE_AVAILABLE and _release_slot_for_dispatch is not None:
                _release_slot_for_dispatch(
                    dstate.dispatch_root(dispatch_root) / _POOL_SLOTS_DIR_NAME,
                    dispatch_id,
                )
        except Exception:
            logger.warning("check_dispatch: slot release failed for %s", dispatch_id)
        text_parts = [f":octagonal_sign: dispatch `{dispatch_id}` killed (operator halt)"]
        mention = _format_agent_mention(agent, agent_user_id)
        if mention:
            text_parts.append(mention)
        await _post(slack_client, channel, thread_ts, " · ".join(text_parts))
        _last_posted.pop(dispatch_id, None)
        return {"status": "done", "reason": "killed"}

    # 3. Budget exceeded — handler wrote `budget` (seconds), supervisor
    # compares elapsed vs budget on every tick. Write a timeout_marker so
    # babysit self-terminates (namespace-safe, no cross-container signal).
    # Mirror the halt path: wait up to 60 s, then fall back to synthetic.
    budget_raw = state.get(dstate.FIELD_BUDGET)
    if started_at is not None and budget_raw:
        try:
            budget = int(budget_raw)
        except ValueError:
            budget = 0
        if budget > 0 and (now - started_at).total_seconds() > budget:
            dstate.write_field(dispatch_id, dstate.FIELD_TIMEOUT_MARKER, now.isoformat(), root=dispatch_root)
            dstate.write_field(dispatch_id, dstate.FIELD_CANCEL_REASON, "runtime_timeout", root=dispatch_root)
            await _wait_for_exitcode(dispatch_id, dispatch_root=dispatch_root)
            _write_synthetic_exitcode_if_absent(dispatch_id, dispatch_root=dispatch_root)
            # Release slot after state writes (preserve state-before-cleanup invariant).
            try:
                if _SLOT_RELEASE_AVAILABLE and _release_slot_for_dispatch is not None:
                    _release_slot_for_dispatch(
                        dstate.dispatch_root(dispatch_root) / _POOL_SLOTS_DIR_NAME,
                        dispatch_id,
                    )
            except Exception:
                logger.warning("check_dispatch: slot release failed for %s", dispatch_id)
            text_parts = [f":alarm_clock: dispatch `{dispatch_id}` timed out after {_format_duration(budget)}"]
            mention = _format_agent_mention(agent, agent_user_id)
            if mention:
                text_parts.append(mention)
            await _post(slack_client, channel, thread_ts, " · ".join(text_parts))
            _last_posted.pop(dispatch_id, None)
            return {"status": "done", "reason": "timeout"}

    # 4. Orphan — heartbeat absent or stale but no exitcode written.
    # Babysit touches <workspace>/heartbeat every ~30 s while running.
    # A missing or stale file means babysit is gone. We use heartbeat
    # instead of os.kill(pid, 0) because the router and agent containers
    # run in separate PID namespaces — signalling a pid from the router
    # namespace produces false positives on live dispatches (#172).
    if not dstate.heartbeat_alive(dispatch_id, root=dispatch_root):
        _write_synthetic_exitcode_if_absent(dispatch_id, dispatch_root=dispatch_root)
        text_parts = [f":ghost: dispatch `{dispatch_id}` orphaned (no exitcode, heartbeat stale)"]
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
