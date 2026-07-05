"""Generic stuck-detection guards for the agent dispatcher.

Implements three failure-mode detectors that run in front of every agent
dispatch and behave the same regardless of which agent is running:

* **Turn cap** — too many turns on a single task. Hard stop.
* **Loop window** — the same tool + normalized args repeats inside a
  sliding window. Catches "calls bash(\"ls\") over and over" patterns.
* **Error streak** — the same error class N times in a row.

Two modes:

* ``dry-run`` (default) — guard records the trip and writes a post-mortem
  but the agent keeps running. Lets us calibrate thresholds against real
  traffic before flipping enforcement on.
* ``enforce`` — guard halts the task. Subsequent dispatches for the same
  task are rejected until ``reset_task`` is called (e.g. a fresh task is
  started, or the operator unwedges manually).

The Slack ``/kill`` command lands here too: it marks a task halted with
``reason: manual_kill`` regardless of mode, so the human always wins.

The guard is intentionally agnostic about *where* turn data comes from.
``StuckGuard.record_turn`` takes plain Python values; the dispatcher feeds
it one event per CLI round-trip (and tests can drive it directly).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Defaults / env var names ──────────────────────────────────────────

MODE_DRY_RUN = "dry-run"
MODE_ENFORCE = "enforce"
DEFAULT_MODE = MODE_DRY_RUN

DEFAULT_TURN_CAP = 50
# Rolling window for turn-cap rate measurement. A scheduled lane producing ~1 turn/hour
# accumulates at most ~1 turn per window tick and never reaches turn_cap. A fast runaway
# (many turns in seconds) fills the window quickly and trips. Must be ≥ the shortest
# scheduler cadence that should never be flagged as stuck (default: 1 hour).
DEFAULT_TURN_CAP_WINDOW = 3600
DEFAULT_LOOP_WINDOW = 5
DEFAULT_LOOP_THRESHOLD = 3
DEFAULT_ERROR_STREAK_THRESHOLD = 3
DEFAULT_POST_MORTEM_DIR = "/config/shared/stuck-tasks"
DEFAULT_MAX_TURNS_STORED = 200

ENV_MODE = "STUCK_GUARD_MODE"
ENV_TURN_CAP = "STUCK_GUARD_TURN_CAP"
ENV_TURN_CAP_WINDOW = "STUCK_GUARD_TURN_CAP_WINDOW"
ENV_LOOP_WINDOW = "STUCK_GUARD_LOOP_WINDOW"
ENV_LOOP_THRESHOLD = "STUCK_GUARD_LOOP_THRESHOLD"
ENV_ERROR_STREAK = "STUCK_GUARD_ERROR_STREAK"
ENV_POST_MORTEM_DIR = "STUCK_GUARD_POST_MORTEM_DIR"
ENV_MAX_TURNS_STORED = "STUCK_GUARD_MAX_TURNS_STORED"

TRIP_TURN_CAP = "turn_cap"
TRIP_LOOP = "loop"
TRIP_ERROR_STREAK = "error_streak"
TRIP_MANUAL_KILL = "manual_kill"


# ── Data shapes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnEvent:
    """A single observed agent turn.

    ``tool_name``/``tool_args_normalized`` are ``None`` when the turn
    didn't end in a tool call (e.g. a final text reply). ``error_class``
    is ``None`` when the turn succeeded.
    """

    timestamp: float
    tool_name: str | None = None
    tool_args_normalized: str | None = None
    error_class: str | None = None


@dataclass(frozen=True)
class GuardConfig:
    """Tunable thresholds + mode for the guard."""

    mode: str = DEFAULT_MODE
    turn_cap: int = DEFAULT_TURN_CAP
    # turn_cap_window_seconds: rolling time window for turn-cap rate measurement.
    # Only turns whose timestamp falls within this many seconds of the most recent
    # turn count toward the cap. Set ≥ longest expected scheduler cadence so that
    # legitimately recurring autonomous lanes never accumulate to the cap.
    turn_cap_window_seconds: int = DEFAULT_TURN_CAP_WINDOW
    loop_window: int = DEFAULT_LOOP_WINDOW
    loop_threshold: int = DEFAULT_LOOP_THRESHOLD
    error_streak_threshold: int = DEFAULT_ERROR_STREAK_THRESHOLD
    post_mortem_dir: str = DEFAULT_POST_MORTEM_DIR
    max_turns_stored: int = DEFAULT_MAX_TURNS_STORED

    def __post_init__(self) -> None:
        if self.mode not in (MODE_DRY_RUN, MODE_ENFORCE):
            raise ValueError(f"Invalid mode {self.mode!r}; expected {MODE_DRY_RUN!r} or {MODE_ENFORCE!r}")
        if self.turn_cap <= 0:
            raise ValueError(f"turn_cap must be > 0 (got {self.turn_cap})")
        if self.turn_cap_window_seconds <= 0:
            raise ValueError(f"turn_cap_window_seconds must be > 0 (got {self.turn_cap_window_seconds})")
        if self.loop_window <= 0:
            raise ValueError(f"loop_window must be > 0 (got {self.loop_window})")
        if self.loop_threshold <= 0:
            raise ValueError(f"loop_threshold must be > 0 (got {self.loop_threshold})")
        if self.loop_threshold > self.loop_window:
            raise ValueError(f"loop_threshold ({self.loop_threshold}) cannot exceed loop_window ({self.loop_window})")
        if self.error_streak_threshold <= 0:
            raise ValueError(f"error_streak_threshold must be > 0 (got {self.error_streak_threshold})")
        if self.max_turns_stored <= 0:
            raise ValueError(f"max_turns_stored must be > 0 (got {self.max_turns_stored})")


@dataclass(frozen=True)
class GuardTrip:
    """A guard tripped — describes which one and what was observed."""

    kind: str
    threshold: int
    observed: int
    description: str


@dataclass
class TaskState:
    """Per-task accumulated state.

    A "task" is a (channel, thread, agent) triple — one Slack thread
    handled by one agent. ``turns`` is the running history (bounded by
    ``GuardConfig.max_turns_stored``); ``turn_count`` is the number of
    automated turns recorded since the last human message (observable metric;
    the turn-cap check uses a rolling time window over ``turns`` instead of
    this counter so scheduled lanes with no human messages never accumulate
    to the cap); ``halted`` flips on guard trip in enforce mode and on
    manual kill in any mode.
    """

    task_id: str
    agent_name: str
    turns: list[TurnEvent] = field(default_factory=list)
    turn_count: int = 0
    halted: bool = False
    halt_reason: GuardTrip | None = None


# ── Helpers ───────────────────────────────────────────────────────────


def normalize_tool_args(args: object) -> str:
    """Stable, comparable string for tool args.

    The loop detector compares this string for equality, so two semantically
    identical calls must produce the same output regardless of dict
    ordering. Non-JSON-serializable values fall back to ``repr()``.
    """
    if args is None:
        return ""
    if isinstance(args, str):
        return args.strip()
    try:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):
        return repr(args)


def make_task_id(channel: str, thread_ts: str, agent_name: str) -> str:
    """Build a stable task identifier from Slack coordinates + agent."""
    return f"{channel}:{thread_ts}:{agent_name}"


def load_config_from_env() -> GuardConfig:
    """Build a :class:`GuardConfig` from the settings layer.

    Values come from the runtime config store with env-var fallback (see
    :mod:`router.settings`). Invalid values are warned about inside the
    settings layer and replaced with the registry defaults rather than
    crashing the router on boot. The guard singleton is built once, so
    changes apply on the next router restart.
    """
    from router import settings  # noqa: PLC0415 — deferred to avoid import cycle

    raw_mode = str(settings.get(ENV_MODE)).strip().lower()
    if raw_mode not in (MODE_DRY_RUN, MODE_ENFORCE):
        logger.warning("Invalid %s=%r; using default %r", ENV_MODE, raw_mode, DEFAULT_MODE)
        raw_mode = DEFAULT_MODE

    return GuardConfig(
        mode=raw_mode,
        turn_cap=settings.get(ENV_TURN_CAP),
        turn_cap_window_seconds=settings.get(ENV_TURN_CAP_WINDOW),
        loop_window=settings.get(ENV_LOOP_WINDOW),
        loop_threshold=settings.get(ENV_LOOP_THRESHOLD),
        error_streak_threshold=settings.get(ENV_ERROR_STREAK),
        post_mortem_dir=settings.get(ENV_POST_MORTEM_DIR),
        max_turns_stored=settings.get(ENV_MAX_TURNS_STORED),
    )


# ── The guard itself ──────────────────────────────────────────────────


class StuckGuard:
    """In-process tracker that decides when to trip on a per-task basis.

    Thread-safe via a single internal lock — the router fans out across
    asyncio tasks but the guard mutations are tiny so a coarse lock is
    fine (and avoids surprising re-entrancy under contention).
    """

    def __init__(self, config: GuardConfig | None = None) -> None:
        self.config = config or GuardConfig()
        self._tasks: dict[str, TaskState] = {}
        self._lock = threading.Lock()
        # Tracks (task_id, trip_kind) pairs that have already triggered a Slack
        # notification in dry-run mode so repeated trips on autonomous lanes do
        # not spam the channel. Cleared per task on reset_task().
        self._notified_trips: set[tuple[str, str]] = set()

    # ── Inspection ────────────────────────────────────────────────────

    def is_halted(self, task_id: str) -> bool:
        with self._lock:
            state = self._tasks.get(task_id)
            return bool(state and state.halted)

    def get_state(self, task_id: str) -> TaskState | None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return None
            # Return a shallow copy so callers can't accidentally mutate
            # the live state from outside the lock.
            return TaskState(
                task_id=state.task_id,
                agent_name=state.agent_name,
                turns=list(state.turns),
                turn_count=state.turn_count,
                halted=state.halted,
                halt_reason=state.halt_reason,
            )

    # ── Mutation ──────────────────────────────────────────────────────

    def reset_task(self, task_id: str) -> None:
        """Clear all guard state for a task. Used when a fresh task starts."""
        with self._lock:
            self._tasks.pop(task_id, None)
            self._notified_trips = {k for k in self._notified_trips if k[0] != task_id}

    def should_notify_trip(self, task_id: str, trip_kind: str) -> bool:
        """Return True if this trip should generate a Slack notification.

        In dry-run mode, each (task_id, trip_kind) pair posts at most once to
        prevent repeat Slack spam when a stuck scheduled lane trips on every
        tick. Enforce-mode trips (which actually halt) and manual kills always
        notify regardless of prior posts.
        """
        if self.config.mode == MODE_ENFORCE or trip_kind == TRIP_MANUAL_KILL:
            return True
        key = (task_id, trip_kind)
        with self._lock:
            if key in self._notified_trips:
                return False
            self._notified_trips.add(key)
            return True

    def record_human_message(self, *, task_id: str, agent_name: str) -> None:
        """Reset the automated turn counter when a human sends a message.

        The turn cap is designed to catch runaway automated loops, not
        human-driven conversations. When a human re-engages in a thread,
        the counter resets so healthy long-lived threads are not permanently
        halted.

        Guard-tripped halts are cleared so the agent can respond again.
        Manual kills (``TRIP_MANUAL_KILL``) are intentionally preserved —
        the human override via ``/kill`` must stay sticky until explicitly
        unwedged via ``reset_task``.

        No-op when the task has not been seen before.
        """
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = self._tasks.setdefault(task_id, TaskState(task_id=task_id, agent_name=agent_name))
            state.agent_name = agent_name
            state.turn_count = 0
            state.turns.clear()
            if state.halted and (state.halt_reason is None or state.halt_reason.kind != TRIP_MANUAL_KILL):
                state.halted = False
                state.halt_reason = None

    def record_turn(
        self,
        *,
        task_id: str,
        agent_name: str,
        tool_name: str | None = None,
        tool_args: object = None,
        error_class: str | None = None,
        timestamp: float | None = None,
    ) -> GuardTrip | None:
        """Append a turn to the task's history and check thresholds.

        Returns the :class:`GuardTrip` that fired, or ``None`` if no
        guard tripped on this turn. The trip is recorded on the task
        state regardless of mode; in enforce mode the task is also marked
        halted.
        """
        ts = timestamp if timestamp is not None else _now_ts()
        event = TurnEvent(
            timestamp=ts,
            tool_name=tool_name,
            tool_args_normalized=normalize_tool_args(tool_args) if tool_name else None,
            error_class=error_class,
        )

        with self._lock:
            state = self._tasks.setdefault(task_id, TaskState(task_id=task_id, agent_name=agent_name))
            # Surface agent renames eagerly — this should be rare but it
            # keeps the post-mortem accurate when a thread changes hands.
            state.agent_name = agent_name
            state.turns.append(event)
            state.turn_count += 1
            # Bound stored history to prevent unbounded memory growth; the
            # turn_count field tracks the actual cap-relevant count.
            if len(state.turns) > self.config.max_turns_stored:
                del state.turns[: len(state.turns) - self.config.max_turns_stored]

            trip = self._evaluate(state)
            if trip is not None and self.config.mode == MODE_ENFORCE:
                state.halted = True
                state.halt_reason = trip
            elif trip is not None:
                # dry-run: still record what tripped so post-mortem readers
                # know why, but leave halted=False so dispatch continues.
                state.halt_reason = trip

            return trip

    def kill(
        self,
        *,
        task_id: str,
        agent_name: str,
        reason: str = TRIP_MANUAL_KILL,
    ) -> GuardTrip:
        """Mark the task halted regardless of mode.

        The Slack ``/kill`` command routes here. Always succeeds, even
        if the task hasn't been seen before — the kill is recorded so a
        subsequent dispatch can be rejected and a post-mortem written.
        """
        trip = GuardTrip(
            kind=TRIP_MANUAL_KILL,
            threshold=0,
            observed=0,
            description=f"Manual kill via Slack ({reason})",
        )
        with self._lock:
            state = self._tasks.setdefault(task_id, TaskState(task_id=task_id, agent_name=agent_name))
            state.agent_name = agent_name
            state.halted = True
            state.halt_reason = trip
        return trip

    # ── Threshold checks ──────────────────────────────────────────────

    def _evaluate(self, state: TaskState) -> GuardTrip | None:
        """Evaluate all guards against the current task state.

        Order matters for the rare case where multiple guards fire on the
        same turn — we report the most severe (turn_cap) first because
        that's the one that's hardest to recover from.
        """
        cfg = self.config

        # Turn-cap uses a rolling time window so that purely autonomous scheduled
        # lanes (no human messages, ~1 turn/hour) never accumulate to the cap,
        # while a fast runaway loop (many turns in seconds) still trips it.
        # The most recent turn is always state.turns[-1] since _evaluate is called
        # immediately after appending the new event.
        now_ts = state.turns[-1].timestamp if state.turns else 0.0
        window_cutoff = now_ts - cfg.turn_cap_window_seconds
        windowed_count = sum(1 for t in state.turns if t.timestamp >= window_cutoff)

        if windowed_count >= cfg.turn_cap:
            return GuardTrip(
                kind=TRIP_TURN_CAP,
                threshold=cfg.turn_cap,
                observed=windowed_count,
                description=(
                    f"Turn cap reached ({windowed_count} >= {cfg.turn_cap}"
                    f" within {cfg.turn_cap_window_seconds}s window)"
                ),
            )

        loop_trip = self._check_loop(state)
        if loop_trip is not None:
            return loop_trip

        error_trip = self._check_error_streak(state)
        if error_trip is not None:
            return error_trip

        return None

    def _check_loop(self, state: TaskState) -> GuardTrip | None:
        """Trip if the same tool + args repeats ``loop_threshold`` times in
        the last ``loop_window`` turns. Turns without a tool call don't
        count toward repetition (they break the bucket only if they
        displace a tool call out of the window)."""
        cfg = self.config
        window = state.turns[-cfg.loop_window :]
        counts: dict[tuple[str, str], int] = {}
        for turn in window:
            if turn.tool_name is None:
                continue
            key = (turn.tool_name, turn.tool_args_normalized or "")
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= cfg.loop_threshold:
                return GuardTrip(
                    kind=TRIP_LOOP,
                    threshold=cfg.loop_threshold,
                    observed=counts[key],
                    description=(
                        f"Tool {turn.tool_name}({turn.tool_args_normalized}) "
                        f"called {counts[key]} times in last {len(window)} turns"
                    ),
                )
        return None

    def _check_error_streak(self, state: TaskState) -> GuardTrip | None:
        """Trip if the most recent ``error_streak_threshold`` turns all
        ended in the same non-empty error class."""
        cfg = self.config
        if len(state.turns) < cfg.error_streak_threshold:
            return None
        tail = state.turns[-cfg.error_streak_threshold :]
        first_error = tail[0].error_class
        if not first_error:
            return None
        if all(turn.error_class == first_error for turn in tail):
            return GuardTrip(
                kind=TRIP_ERROR_STREAK,
                threshold=cfg.error_streak_threshold,
                observed=cfg.error_streak_threshold,
                description=f"Error class {first_error!r} repeated {cfg.error_streak_threshold} turns in a row",
            )
        return None


def _now_ts() -> float:
    """Wall-clock seconds. Wrapped for ease of mocking in tests."""
    return datetime.now(tz=timezone.utc).timestamp()


# ── Post-mortem + Slack formatting ────────────────────────────────────


def _slugify_agent(agent_name: str) -> str:
    """Filesystem-safe rendering of an agent name."""
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in agent_name.lower())
    return safe or "unknown"


def _format_turn_for_post_mortem(turn: TurnEvent) -> str:
    parts = [datetime.fromtimestamp(turn.timestamp, tz=timezone.utc).isoformat()]
    if turn.tool_name is not None:
        parts.append(f"tool={turn.tool_name}")
        if turn.tool_args_normalized:
            parts.append(f"args={turn.tool_args_normalized}")
    else:
        parts.append("tool=<none>")
    if turn.error_class:
        parts.append(f"error={turn.error_class}")
    return " | ".join(parts)


def write_post_mortem(
    *,
    state: TaskState,
    trip: GuardTrip,
    config: GuardConfig,
    slack_thread_url: str | None = None,
    task_description: str | None = None,
    timestamp: datetime | None = None,
    last_n_turns: int = 5,
) -> Path:
    """Write a markdown post-mortem and return the path.

    Creates the post-mortem directory with mode 0700 if missing — these
    files can contain user-supplied tool args, so they should not be
    world-readable. Filename pattern: ``<agent>-<isoZ>.md``.
    """
    when = timestamp or datetime.now(tz=timezone.utc)
    iso = when.strftime("%Y%m%dT%H%M%SZ")

    out_dir = Path(config.post_mortem_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's `mode` is only honoured on creation; tighten an existing
    # directory's perms too so we never leak history through a previously
    # mis-created dir.
    try:
        os.chmod(out_dir, 0o700)
    except OSError:
        # Best-effort — some test filesystems don't support chmod.
        logger.debug("Could not chmod post-mortem dir %s", out_dir)

    path = out_dir / f"{_slugify_agent(state.agent_name)}-{iso}.md"

    tail = state.turns[-last_n_turns:] if last_n_turns > 0 else state.turns
    lines = [
        f"# Stuck-guard trip: {state.agent_name}",
        "",
        f"- **When:** {when.isoformat()}",
        f"- **Mode:** {config.mode}",
        f"- **Task ID:** `{state.task_id}`",
        f"- **Trip kind:** {trip.kind}",
        f"- **Threshold:** {trip.threshold}",
        f"- **Observed:** {trip.observed}",
        f"- **Description:** {trip.description}",
    ]
    if slack_thread_url:
        lines.append(f"- **Slack thread:** {slack_thread_url}")
    if task_description:
        lines.append(f"- **Task description:** {task_description}")
    lines += [
        "",
        f"## Last {len(tail)} turn(s)",
        "",
    ]
    if tail:
        for turn in tail:
            lines.append(f"- {_format_turn_for_post_mortem(turn)}")
    else:
        lines.append("_(no turns recorded)_")
    lines.append("")

    path.write_text("\n".join(lines))
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("Could not chmod post-mortem file %s", path)

    logger.info("Wrote stuck-guard post-mortem to %s", path)
    return path


def format_slack_message(
    *,
    state: TaskState,
    trip: GuardTrip,
    post_mortem_path: Path | str | None,
    config: GuardConfig,
) -> str:
    """Build the short Slack notification text for a trip.

    Tagged ``[DRY-RUN]`` when the guard is in dry-run mode so on-call
    can tell at a glance whether the agent actually stopped or kept going.
    """
    tag = "[DRY-RUN] " if config.mode == MODE_DRY_RUN and trip.kind != TRIP_MANUAL_KILL else ""
    location = f"\nPost-mortem: `{post_mortem_path}`" if post_mortem_path else ""
    return f":warning: {tag}Stuck guard tripped for *{state.agent_name}* (`{trip.kind}`): {trip.description}.{location}"


# ── Module-level singleton (lazy) ─────────────────────────────────────


_default_guard: StuckGuard | None = None
_default_guard_lock = threading.Lock()


def get_default_guard() -> StuckGuard:
    """Return the process-wide :class:`StuckGuard`, creating it on demand.

    Loads :class:`GuardConfig` from environment on first access. Use
    :func:`reset_default_guard` in tests that need a fresh instance.
    """
    global _default_guard
    if _default_guard is None:
        with _default_guard_lock:
            if _default_guard is None:
                _default_guard = StuckGuard(load_config_from_env())
    return _default_guard


def reset_default_guard(guard: StuckGuard | None = None) -> StuckGuard:
    """Replace the module-level guard (test hook).

    Pass an explicit guard to install it, or omit the argument to clear
    the cache so the next ``get_default_guard()`` rebuilds from env.
    """
    global _default_guard
    with _default_guard_lock:
        _default_guard = guard
    return guard if guard is not None else get_default_guard()
