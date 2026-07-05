"""Unit tests for router.stuck_guard.

Covers the threshold checks (turn cap, loop, error streak), kill switch,
config loading, post-mortem writing, and Slack message formatting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from router.stuck_guard import (
    DEFAULT_ERROR_STREAK_THRESHOLD,
    DEFAULT_LOOP_THRESHOLD,
    DEFAULT_LOOP_WINDOW,
    DEFAULT_MAX_TURNS_STORED,
    DEFAULT_TURN_CAP,
    DEFAULT_TURN_CAP_WINDOW,
    ENV_ERROR_STREAK,
    ENV_LOOP_THRESHOLD,
    ENV_LOOP_WINDOW,
    ENV_MAX_TURNS_STORED,
    ENV_MODE,
    ENV_POST_MORTEM_DIR,
    ENV_TURN_CAP,
    ENV_TURN_CAP_WINDOW,
    MODE_DRY_RUN,
    MODE_ENFORCE,
    TRIP_ERROR_STREAK,
    TRIP_LOOP,
    TRIP_MANUAL_KILL,
    TRIP_TURN_CAP,
    GuardConfig,
    StuckGuard,
    format_slack_message,
    get_default_guard,
    load_config_from_env,
    make_task_id,
    normalize_tool_args,
    reset_default_guard,
    write_post_mortem,
)

pytestmark = pytest.mark.unit


# ── GuardConfig validation ────────────────────────────────────────────


class TestGuardConfigValidation:
    def test_default_config_passes(self):
        cfg = GuardConfig()
        assert cfg.mode == MODE_DRY_RUN
        assert cfg.turn_cap == DEFAULT_TURN_CAP
        assert cfg.loop_window == DEFAULT_LOOP_WINDOW
        assert cfg.loop_threshold == DEFAULT_LOOP_THRESHOLD
        assert cfg.error_streak_threshold == DEFAULT_ERROR_STREAK_THRESHOLD

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            GuardConfig(mode="loud")

    def test_zero_turn_cap_rejected(self):
        with pytest.raises(ValueError, match="turn_cap"):
            GuardConfig(turn_cap=0)

    def test_zero_loop_window_rejected(self):
        with pytest.raises(ValueError, match="loop_window"):
            GuardConfig(loop_window=0)

    def test_loop_threshold_exceeds_window_rejected(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            GuardConfig(loop_window=3, loop_threshold=4)

    def test_negative_error_streak_rejected(self):
        with pytest.raises(ValueError, match="error_streak"):
            GuardConfig(error_streak_threshold=0)

    def test_zero_max_turns_stored_rejected(self):
        with pytest.raises(ValueError, match="max_turns_stored"):
            GuardConfig(max_turns_stored=0)

    def test_default_turn_cap_window(self):
        cfg = GuardConfig()
        assert cfg.turn_cap_window_seconds == DEFAULT_TURN_CAP_WINDOW

    def test_zero_turn_cap_window_rejected(self):
        with pytest.raises(ValueError, match="turn_cap_window_seconds"):
            GuardConfig(turn_cap_window_seconds=0)


# ── Env var loading ───────────────────────────────────────────────────


class TestLoadConfigFromEnv:
    def test_defaults_when_unset(self, monkeypatch):
        for key in [ENV_MODE, ENV_TURN_CAP, ENV_LOOP_WINDOW, ENV_LOOP_THRESHOLD, ENV_ERROR_STREAK, ENV_POST_MORTEM_DIR]:
            monkeypatch.delenv(key, raising=False)
        cfg = load_config_from_env()
        assert cfg.mode == MODE_DRY_RUN
        assert cfg.turn_cap == DEFAULT_TURN_CAP

    def test_enforce_mode_from_env(self, monkeypatch):
        monkeypatch.setenv(ENV_MODE, "enforce")
        cfg = load_config_from_env()
        assert cfg.mode == MODE_ENFORCE

    def test_invalid_mode_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(ENV_MODE, "loud")
        cfg = load_config_from_env()
        assert cfg.mode == MODE_DRY_RUN

    def test_int_envs_parsed(self, monkeypatch):
        monkeypatch.setenv(ENV_TURN_CAP, "10")
        monkeypatch.setenv(ENV_TURN_CAP_WINDOW, "7200")
        monkeypatch.setenv(ENV_LOOP_WINDOW, "4")
        monkeypatch.setenv(ENV_LOOP_THRESHOLD, "2")
        monkeypatch.setenv(ENV_ERROR_STREAK, "5")
        cfg = load_config_from_env()
        assert cfg.turn_cap == 10
        assert cfg.turn_cap_window_seconds == 7200
        assert cfg.loop_window == 4
        assert cfg.loop_threshold == 2
        assert cfg.error_streak_threshold == 5

    def test_turn_cap_window_from_env(self, monkeypatch):
        monkeypatch.setenv(ENV_TURN_CAP_WINDOW, "1800")
        cfg = load_config_from_env()
        assert cfg.turn_cap_window_seconds == 1800

    def test_turn_cap_window_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(ENV_TURN_CAP_WINDOW, raising=False)
        cfg = load_config_from_env()
        assert cfg.turn_cap_window_seconds == DEFAULT_TURN_CAP_WINDOW

    def test_invalid_int_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(ENV_TURN_CAP, "not-a-number")
        cfg = load_config_from_env()
        assert cfg.turn_cap == DEFAULT_TURN_CAP

    def test_zero_int_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(ENV_TURN_CAP, "0")
        cfg = load_config_from_env()
        assert cfg.turn_cap == DEFAULT_TURN_CAP

    def test_post_mortem_dir_overridable(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_POST_MORTEM_DIR, str(tmp_path))
        cfg = load_config_from_env()
        assert cfg.post_mortem_dir == str(tmp_path)

    def test_max_turns_stored_parsed(self, monkeypatch):
        monkeypatch.setenv(ENV_MAX_TURNS_STORED, "100")
        cfg = load_config_from_env()
        assert cfg.max_turns_stored == 100

    def test_defaults_include_max_turns_stored(self, monkeypatch):
        monkeypatch.delenv(ENV_MAX_TURNS_STORED, raising=False)
        cfg = load_config_from_env()
        assert cfg.max_turns_stored == DEFAULT_MAX_TURNS_STORED


# ── normalize_tool_args ───────────────────────────────────────────────


class TestNormalizeToolArgs:
    def test_none_returns_empty(self):
        assert normalize_tool_args(None) == ""

    def test_string_passthrough_trim(self):
        assert normalize_tool_args("  ls -la  ") == "ls -la"

    def test_dict_keys_sorted(self):
        a = normalize_tool_args({"b": 1, "a": 2})
        b = normalize_tool_args({"a": 2, "b": 1})
        assert a == b

    def test_non_serializable_falls_back_to_repr(self):
        class Weird:
            def __repr__(self):
                return "<weird>"

        # default=repr in json.dumps still wraps the repr in quotes,
        # so we just need a stable result regardless of dict ordering.
        out = normalize_tool_args({"x": Weird()})
        assert "weird" in out


# ── Turn cap guard ────────────────────────────────────────────────────


class TestTurnCap:
    def test_below_cap_does_not_trip(self):
        guard = StuckGuard(GuardConfig(turn_cap=3))
        assert guard.record_turn(task_id="t1", agent_name="lisa") is None
        assert guard.record_turn(task_id="t1", agent_name="lisa") is None
        # Still 2/3 — no trip yet.
        assert not guard.is_halted("t1")

    def test_at_cap_trips(self):
        guard = StuckGuard(GuardConfig(turn_cap=3, mode=MODE_ENFORCE))
        guard.record_turn(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t1", agent_name="lisa")
        trip = guard.record_turn(task_id="t1", agent_name="lisa")
        assert trip is not None
        assert trip.kind == TRIP_TURN_CAP
        assert trip.observed == 3
        assert trip.threshold == 3
        assert guard.is_halted("t1")

    def test_dry_run_does_not_halt_on_cap(self):
        guard = StuckGuard(GuardConfig(turn_cap=2, mode=MODE_DRY_RUN))
        guard.record_turn(task_id="t1", agent_name="lisa")
        trip = guard.record_turn(task_id="t1", agent_name="lisa")
        assert trip is not None
        assert not guard.is_halted("t1")

    def test_separate_tasks_have_independent_counters(self):
        guard = StuckGuard(GuardConfig(turn_cap=2, mode=MODE_ENFORCE))
        guard.record_turn(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t2", agent_name="lisa")
        # Neither has hit the cap on its own.
        assert not guard.is_halted("t1")
        assert not guard.is_halted("t2")

    def test_acceptance_49_turns_does_not_trip(self):
        # "Agent at 49 turns with varied work → does not trip."
        guard = StuckGuard(GuardConfig(turn_cap=50, loop_window=5, loop_threshold=3))
        # Use distinct tools to avoid loop trip
        for i in range(49):
            trip = guard.record_turn(
                task_id="t1",
                agent_name="lisa",
                tool_name=f"tool_{i % 50}",
                tool_args={"i": i},
            )
            assert trip is None or trip.kind != TRIP_TURN_CAP
        assert not guard.is_halted("t1")


# ── Loop guard ────────────────────────────────────────────────────────


class TestLoopGuard:
    def test_three_identical_in_five_trips(self):
        # Acceptance: "Agent calls bash('ls') 3 times in last 5 turns"
        guard = StuckGuard(GuardConfig(turn_cap=100, loop_window=5, loop_threshold=3, mode=MODE_ENFORCE))
        # Pad with non-tool turns to lengthen the window.
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        assert trip is not None
        assert trip.kind == TRIP_LOOP
        assert trip.observed == 3

    def test_five_distinct_tools_does_not_trip(self):
        # Acceptance: "Agent calls 5 different tools in 5 turns → loop guard does not trip."
        guard = StuckGuard(GuardConfig(turn_cap=100, loop_window=5, loop_threshold=3))
        for tool in ["bash", "read", "write", "edit", "search"]:
            trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name=tool, tool_args={"x": 1})
            assert trip is None or trip.kind != TRIP_LOOP

    def test_args_normalized_for_loop_detection(self):
        # Same call expressed with different dict ordering should still trip.
        guard = StuckGuard(GuardConfig(turn_cap=100, loop_window=5, loop_threshold=3))
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="http", tool_args={"a": 1, "b": 2})
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="http", tool_args={"b": 2, "a": 1})
        trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name="http", tool_args={"a": 1, "b": 2})
        assert trip is not None
        assert trip.kind == TRIP_LOOP

    def test_old_repeats_outside_window_do_not_trip(self):
        # Two repeats early, then padded out of the window — must not trip.
        guard = StuckGuard(GuardConfig(turn_cap=100, loop_window=3, loop_threshold=3))
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="other_a")
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="other_b")
        # Window now contains the two `other_*` plus the next one — only
        # one bash in the window.
        trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        assert trip is None or trip.kind != TRIP_LOOP


# ── Error streak guard ────────────────────────────────────────────────


class TestErrorStreak:
    def test_three_same_errors_trips(self):
        # Acceptance: "Agent gets the same FileNotFoundError 3 turns in a row"
        guard = StuckGuard(GuardConfig(error_streak_threshold=3, mode=MODE_ENFORCE))
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        trip = guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        assert trip is not None
        assert trip.kind == TRIP_ERROR_STREAK
        assert "FileNotFoundError" in trip.description

    def test_three_different_errors_does_not_trip(self):
        # Acceptance: "Agent gets 3 different errors → error-streak guard does not trip."
        guard = StuckGuard(GuardConfig(error_streak_threshold=3))
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="ValueError")
        trip = guard.record_turn(task_id="t1", agent_name="lisa", error_class="KeyError")
        assert trip is None or trip.kind != TRIP_ERROR_STREAK

    def test_streak_broken_by_success(self):
        guard = StuckGuard(GuardConfig(error_streak_threshold=3))
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        guard.record_turn(task_id="t1", agent_name="lisa", error_class=None)  # success
        trip = guard.record_turn(task_id="t1", agent_name="lisa", error_class="FileNotFoundError")
        assert trip is None or trip.kind != TRIP_ERROR_STREAK


# ── Kill switch ───────────────────────────────────────────────────────


class TestKillSwitch:
    def test_kill_halts_in_dry_run(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        trip = guard.kill(task_id="t1", agent_name="sam")
        assert trip.kind == TRIP_MANUAL_KILL
        # Acceptance: "Kill works in both dry-run and enforce mode."
        assert guard.is_halted("t1")

    def test_kill_halts_in_enforce(self):
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE))
        guard.kill(task_id="t1", agent_name="sam")
        assert guard.is_halted("t1")

    def test_kill_creates_state_for_unseen_task(self):
        guard = StuckGuard()
        guard.kill(task_id="brand-new", agent_name="dave")
        state = guard.get_state("brand-new")
        assert state is not None
        assert state.agent_name == "dave"
        assert state.halted is True
        assert state.halt_reason is not None
        assert state.halt_reason.kind == TRIP_MANUAL_KILL


# ── reset_task ────────────────────────────────────────────────────────


class TestResetTask:
    def test_reset_clears_halt(self):
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE))
        guard.kill(task_id="t1", agent_name="sam")
        assert guard.is_halted("t1")
        guard.reset_task("t1")
        assert not guard.is_halted("t1")
        assert guard.get_state("t1") is None

    def test_reset_unknown_task_is_noop(self):
        guard = StuckGuard()
        guard.reset_task("never-existed")  # must not raise


# ── Mode behavior on trip ─────────────────────────────────────────────


class TestModeBehavior:
    def test_dry_run_records_trip_but_does_not_halt(self):
        guard = StuckGuard(GuardConfig(turn_cap=2, mode=MODE_DRY_RUN))
        guard.record_turn(task_id="t1", agent_name="lisa")
        trip = guard.record_turn(task_id="t1", agent_name="lisa")
        assert trip is not None
        assert not guard.is_halted("t1")
        # Halt reason still recorded for the post-mortem reader.
        state = guard.get_state("t1")
        assert state is not None
        assert state.halt_reason is not None

    def test_enforce_halts_on_trip(self):
        guard = StuckGuard(GuardConfig(turn_cap=2, mode=MODE_ENFORCE))
        guard.record_turn(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t1", agent_name="lisa")
        assert guard.is_halted("t1")


# ── Post-mortem writer ────────────────────────────────────────────────


class TestPostMortemWriter:
    def test_writes_file_with_expected_fields(self, tmp_path):
        cfg = GuardConfig(post_mortem_dir=str(tmp_path), mode=MODE_ENFORCE)
        guard = StuckGuard(cfg)
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        trip = guard.kill(task_id="t1", agent_name="lisa")
        state = guard.get_state("t1")

        path = write_post_mortem(
            state=state,
            trip=trip,
            config=cfg,
            slack_thread_url="https://slack/test",
            task_description="Find the bug",
        )
        assert Path(path).exists()
        content = Path(path).read_text()
        assert "lisa" in content
        assert "manual_kill" in content
        assert "https://slack/test" in content
        assert "Find the bug" in content
        assert "bash" in content

    def test_handles_missing_directory(self, tmp_path):
        nested = tmp_path / "nested" / "stuck"
        cfg = GuardConfig(post_mortem_dir=str(nested))
        guard = StuckGuard(cfg)
        guard.record_turn(task_id="t1", agent_name="sam", error_class="X")
        guard.record_turn(task_id="t1", agent_name="sam", error_class="X")
        trip = guard.record_turn(task_id="t1", agent_name="sam", error_class="X")
        state = guard.get_state("t1")
        path = write_post_mortem(state=state, trip=trip, config=cfg)
        assert Path(path).exists()
        assert nested.exists()


# ── Slack message formatting ──────────────────────────────────────────


class TestSlackMessage:
    def test_dry_run_tagged(self):
        cfg = GuardConfig(mode=MODE_DRY_RUN)
        guard = StuckGuard(cfg)
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="X")
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="X")
        trip = guard.record_turn(task_id="t1", agent_name="lisa", error_class="X")
        state = guard.get_state("t1")
        msg = format_slack_message(state=state, trip=trip, post_mortem_path="/tmp/foo.md", config=cfg)
        assert "[DRY-RUN]" in msg
        assert "lisa" in msg

    def test_enforce_not_tagged(self):
        cfg = GuardConfig(mode=MODE_ENFORCE)
        guard = StuckGuard(cfg)
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="X")
        guard.record_turn(task_id="t1", agent_name="lisa", error_class="X")
        trip = guard.record_turn(task_id="t1", agent_name="lisa", error_class="X")
        state = guard.get_state("t1")
        msg = format_slack_message(state=state, trip=trip, post_mortem_path="/tmp/foo.md", config=cfg)
        assert "[DRY-RUN]" not in msg

    def test_manual_kill_never_tagged_dry_run(self):
        # A kill is a kill regardless of mode — don't tag it [DRY-RUN] or
        # operators will doubt whether the agent really stopped.
        cfg = GuardConfig(mode=MODE_DRY_RUN)
        guard = StuckGuard(cfg)
        trip = guard.kill(task_id="t1", agent_name="lisa")
        state = guard.get_state("t1")
        msg = format_slack_message(state=state, trip=trip, post_mortem_path=None, config=cfg)
        assert "[DRY-RUN]" not in msg
        assert "manual_kill" in msg


# ── Default guard singleton ───────────────────────────────────────────


class TestDefaultGuard:
    def test_get_default_guard_caches(self, monkeypatch):
        reset_default_guard(None)
        monkeypatch.delenv(ENV_MODE, raising=False)
        g1 = get_default_guard()
        g2 = get_default_guard()
        assert g1 is g2
        reset_default_guard(None)

    def test_reset_replaces_instance(self):
        custom = StuckGuard(GuardConfig(mode=MODE_ENFORCE))
        reset_default_guard(custom)
        assert get_default_guard() is custom
        reset_default_guard(None)


# ── Helpers ──────────────────────────────────────────────────────────


class TestMakeTaskId:
    def test_includes_agent(self):
        tid = make_task_id("C1", "1.1", "lisa")
        assert "lisa" in tid
        assert "C1" in tid
        assert "1.1" in tid

    def test_distinct_agents_distinct_ids(self):
        # Two agents in the same thread must have distinct task IDs so
        # one's loop doesn't get blamed on the other.
        a = make_task_id("C1", "1.1", "lisa")
        b = make_task_id("C1", "1.1", "sam")
        assert a != b


# ── Human message reset ───────────────────────────────────────────────


class TestHumanMessageReset:
    def test_human_message_resets_turn_count(self):
        guard = StuckGuard(GuardConfig(turn_cap=5, mode=MODE_ENFORCE))
        for _ in range(4):
            guard.record_turn(task_id="t1", agent_name="lisa")
        state = guard.get_state("t1")
        assert state.turn_count == 4

        guard.record_human_message(task_id="t1", agent_name="lisa")

        state = guard.get_state("t1")
        assert state.turn_count == 0

    def test_human_message_prevents_turn_cap_on_long_thread(self):
        # Acceptance: a 60-turn thread that gets a human message mid-way
        # must not trip the 50-turn cap if the post-reset segment is short.
        # Use distinct tool args so the loop guard does not fire.
        guard = StuckGuard(GuardConfig(turn_cap=50, mode=MODE_ENFORCE))
        # Record 49 automated turns — just under the cap.
        for i in range(49):
            trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name=f"tool_{i % 50}", tool_args={"i": i})
            assert trip is None or trip.kind != TRIP_TURN_CAP
        # Human sends a message — counter resets.
        guard.record_human_message(task_id="t1", agent_name="lisa")
        # 10 more automated turns must NOT trip the cap.
        for i in range(10):
            trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name=f"post_{i}", tool_args={"i": i})
            assert trip is None or trip.kind != TRIP_TURN_CAP
        assert not guard.is_halted("t1")

    def test_human_message_clears_guard_tripped_halt(self):
        # A guard-trip halt (e.g. turn_cap in enforce mode) must be cleared
        # when the human re-engages.
        guard = StuckGuard(GuardConfig(turn_cap=2, mode=MODE_ENFORCE))
        guard.record_turn(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t1", agent_name="lisa")
        assert guard.is_halted("t1")

        guard.record_human_message(task_id="t1", agent_name="lisa")

        assert not guard.is_halted("t1")
        state = guard.get_state("t1")
        assert state.halt_reason is None

    def test_human_message_does_not_clear_manual_kill(self):
        # A manual /kill must survive a human message — it is intentional
        # and must remain until explicitly unwedged via reset_task.
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.kill(task_id="t1", agent_name="lisa")
        assert guard.is_halted("t1")

        guard.record_human_message(task_id="t1", agent_name="lisa")

        assert guard.is_halted("t1")
        state = guard.get_state("t1")
        assert state.halt_reason is not None
        assert state.halt_reason.kind == TRIP_MANUAL_KILL

    def test_human_message_on_unseen_task_creates_state(self):
        guard = StuckGuard()
        guard.record_human_message(task_id="brand-new", agent_name="sam")
        state = guard.get_state("brand-new")
        assert state is not None
        assert state.turn_count == 0
        assert not state.halted

    def test_turn_cap_still_catches_runaway_after_reset(self):
        # After a human reset the cap still fires if the agent runs away.
        guard = StuckGuard(GuardConfig(turn_cap=3, mode=MODE_ENFORCE))
        guard.record_human_message(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t1", agent_name="lisa")
        trip = guard.record_turn(task_id="t1", agent_name="lisa")
        assert trip is not None
        assert trip.kind == TRIP_TURN_CAP
        assert guard.is_halted("t1")

    def test_turn_count_visible_in_get_state(self):
        guard = StuckGuard(GuardConfig(turn_cap=10))
        guard.record_turn(task_id="t1", agent_name="lisa")
        guard.record_turn(task_id="t1", agent_name="lisa")
        state = guard.get_state("t1")
        assert state.turn_count == 2


# ── Bounded turns list ────────────────────────────────────────────────


class TestBoundedTurns:
    def test_turns_list_bounded_to_max_turns_stored(self):
        guard = StuckGuard(GuardConfig(turn_cap=1000, max_turns_stored=5))
        for i in range(20):
            guard.record_turn(task_id="t1", agent_name="lisa", tool_name=f"tool_{i}")
        state = guard.get_state("t1")
        assert len(state.turns) == 5

    def test_bounded_list_preserves_most_recent_turns(self):
        guard = StuckGuard(GuardConfig(turn_cap=1000, max_turns_stored=3))
        for i in range(10):
            guard.record_turn(task_id="t1", agent_name="lisa", tool_name=f"tool_{i}")
        state = guard.get_state("t1")
        stored_names = [t.tool_name for t in state.turns]
        assert stored_names == ["tool_7", "tool_8", "tool_9"]

    def test_turn_count_exceeds_max_turns_stored(self):
        # turn_count tracks all turns even when stored list is smaller.
        guard = StuckGuard(GuardConfig(turn_cap=1000, max_turns_stored=5))
        for _ in range(20):
            guard.record_turn(task_id="t1", agent_name="lisa")
        state = guard.get_state("t1")
        assert state.turn_count == 20
        assert len(state.turns) == 5

    def test_turn_cap_fires_for_rapid_burst_within_window(self):
        # The cap fires when turn_cap rapid-fire turns all fall within the window.
        # max_turns_stored must be >= turn_cap so the stored history contains all
        # window-relevant turns.
        guard = StuckGuard(GuardConfig(turn_cap=5, max_turns_stored=10, mode=MODE_ENFORCE))
        t_base = 1_000_000.0
        for i in range(4):
            trip = guard.record_turn(task_id="t1", agent_name="lisa", timestamp=t_base + i)
            assert trip is None or trip.kind != TRIP_TURN_CAP
        trip = guard.record_turn(task_id="t1", agent_name="lisa", timestamp=t_base + 4)
        assert trip is not None
        assert trip.kind == TRIP_TURN_CAP
        assert trip.observed == 5

    def test_loop_guard_still_works_within_bounded_list(self):
        # The loop guard must still trip even with a small stored window.
        guard = StuckGuard(
            GuardConfig(turn_cap=1000, loop_window=5, loop_threshold=3, max_turns_stored=10, mode=MODE_ENFORCE)
        )
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        trip = guard.record_turn(task_id="t1", agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        assert trip is not None
        assert trip.kind == TRIP_LOOP


# ── Turn-cap rate window (issue #687) ────────────────────────────────


class TestTurnCapWindow:
    """Verify the rate-windowed turn-cap semantics introduced in #687.

    A purely autonomous scheduled/system lane (no human messages, ~1 turn/hour)
    must never trip turn_cap. A fast runaway (many turns in rapid succession)
    must still trip.
    """

    def test_scheduled_lane_never_trips(self):
        # N turns each spaced > turn_cap_window_seconds apart must never trip,
        # even with no human message and N >> turn_cap.
        cap = 5
        window = 60  # 1-minute window for the test
        guard = StuckGuard(GuardConfig(turn_cap=cap, turn_cap_window_seconds=window, max_turns_stored=200))
        # Space each turn 120 s apart (2× the window) so only 1 turn is ever in-window.
        t_base = 1_000_000.0
        spacing = 120.0
        for i in range(cap + 5):  # more than cap turns, all spaced widely
            trip = guard.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + i * spacing)
            assert trip is None or trip.kind != TRIP_TURN_CAP, f"Unexpected turn_cap trip on turn {i}"

    def test_fast_runaway_trips(self):
        # turn_cap turns in rapid succession (within the window) must trip.
        cap = 5
        window = 3600
        guard = StuckGuard(GuardConfig(turn_cap=cap, turn_cap_window_seconds=window, mode=MODE_ENFORCE))
        t_base = 1_000_000.0
        for i in range(cap - 1):
            trip = guard.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + i)
            assert trip is None or trip.kind != TRIP_TURN_CAP
        trip = guard.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + cap - 1)
        assert trip is not None
        assert trip.kind == TRIP_TURN_CAP
        assert trip.observed == cap

    def test_window_is_configurable(self):
        # A small window trips sooner than a large window given the same spacing.
        small_window = 10
        large_window = 3600
        cap = 3
        t_base = 1_000_000.0
        spacing = 20.0  # > small_window, < large_window — only trips small window

        guard_small = StuckGuard(GuardConfig(turn_cap=cap, turn_cap_window_seconds=small_window, mode=MODE_ENFORCE))
        guard_large = StuckGuard(GuardConfig(turn_cap=cap, turn_cap_window_seconds=large_window, mode=MODE_ENFORCE))
        # Record cap turns with 20s spacing into both guards.
        for i in range(cap):
            guard_small.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + i * spacing)
            guard_large.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + i * spacing)

        # With small_window=10s and spacing=20s, only 1 turn is in the window → no trip.
        assert not guard_small.is_halted("t1")
        # With large_window=3600s and spacing=20s, all cap turns are in the window → trip.
        assert guard_large.is_halted("t1")

    def test_human_reset_clears_window(self):
        # After record_human_message, the stored turns are cleared so the windowed
        # count resets to 0. #422 behavior preserved.
        cap = 3
        guard = StuckGuard(GuardConfig(turn_cap=cap, mode=MODE_ENFORCE))
        t_base = 1_000_000.0
        for i in range(cap - 1):
            guard.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + i)
        guard.record_human_message(task_id="t1", agent_name="sam")
        # After reset, cap - 1 more turns must not trip.
        for i in range(cap - 1):
            trip = guard.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + 100 + i)
            assert trip is None or trip.kind != TRIP_TURN_CAP

    def test_default_window_large_enough_for_hourly_scheduler(self):
        # Hourly scheduler: 1 turn/hour. With DEFAULT_TURN_CAP_WINDOW (1 hour) and
        # DEFAULT_TURN_CAP (50), at most ~1-2 turns land in any 1-hour window → no trip.
        cap = DEFAULT_TURN_CAP
        window = DEFAULT_TURN_CAP_WINDOW  # 3600 s
        guard = StuckGuard(GuardConfig(turn_cap=cap, turn_cap_window_seconds=window, max_turns_stored=200))
        t_base = 1_000_000.0
        spacing = 3600.0  # exactly 1 hour between turns
        for i in range(cap + 10):
            trip = guard.record_turn(task_id="t1", agent_name="sam", timestamp=t_base + i * spacing)
            assert trip is None or trip.kind != TRIP_TURN_CAP, f"Unexpected trip at turn {i}"


# ── Dry-run notification dedup (issue #687) ───────────────────────────


class TestDryRunDedup:
    """Verify should_notify_trip() deduplicates Slack posts in dry-run mode."""

    def test_first_trip_notifies(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        assert guard.should_notify_trip("t1", TRIP_TURN_CAP) is True

    def test_repeat_trip_suppressed(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.should_notify_trip("t1", TRIP_TURN_CAP)
        assert guard.should_notify_trip("t1", TRIP_TURN_CAP) is False

    def test_different_trip_kind_notifies(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.should_notify_trip("t1", TRIP_TURN_CAP)
        # A different trip kind for the same task is a new notification.
        assert guard.should_notify_trip("t1", TRIP_LOOP) is True

    def test_different_task_notifies(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.should_notify_trip("t1", TRIP_TURN_CAP)
        assert guard.should_notify_trip("t2", TRIP_TURN_CAP) is True

    def test_enforce_mode_always_notifies(self):
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE))
        guard.should_notify_trip("t1", TRIP_TURN_CAP)
        # Even the second call notifies in enforce mode.
        assert guard.should_notify_trip("t1", TRIP_TURN_CAP) is True

    def test_manual_kill_always_notifies_in_dry_run(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.should_notify_trip("t1", TRIP_MANUAL_KILL)
        assert guard.should_notify_trip("t1", TRIP_MANUAL_KILL) is True

    def test_reset_task_clears_dedup(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.should_notify_trip("t1", TRIP_TURN_CAP)
        assert guard.should_notify_trip("t1", TRIP_TURN_CAP) is False
        guard.reset_task("t1")
        # After reset, the same (task_id, trip_kind) should notify again.
        assert guard.should_notify_trip("t1", TRIP_TURN_CAP) is True

    def test_reset_does_not_affect_other_tasks(self):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN))
        guard.should_notify_trip("t1", TRIP_TURN_CAP)
        guard.should_notify_trip("t2", TRIP_TURN_CAP)
        guard.reset_task("t1")
        # t2's dedup state should be unaffected.
        assert guard.should_notify_trip("t2", TRIP_TURN_CAP) is False


# ── Human-rescue regression (issue #485) ─────────────────────────────


class TestHumanRescueRegressions:
    """Verify the human-rescue escape hatch works for loop and error_streak
    in enforce mode — the bug fixed in #485."""

    def test_rescued_loop_trip_does_not_re_halt_on_next_turn(self):
        # Trip the loop guard, rescue via human message, then send one
        # non-repeating tool call — the thread must NOT re-halt.
        guard = StuckGuard(GuardConfig(turn_cap=100, loop_window=5, loop_threshold=3, mode=MODE_ENFORCE))
        task_id = "t1"
        for _ in range(3):
            guard.record_turn(task_id=task_id, agent_name="lisa", tool_name="bash", tool_args={"cmd": "ls"})
        assert guard.is_halted(task_id)

        guard.record_human_message(task_id=task_id, agent_name="lisa")
        assert not guard.is_halted(task_id)

        trip = guard.record_turn(task_id=task_id, agent_name="lisa", tool_name="read", tool_args={"path": "/foo"})
        assert trip is None
        assert not guard.is_halted(task_id)

    def test_rescued_error_streak_trip_does_not_re_halt_on_next_turn(self):
        # Trip the error_streak guard, rescue via human message, then send
        # one clean turn — the thread must NOT re-halt.
        guard = StuckGuard(GuardConfig(turn_cap=100, error_streak_threshold=3, mode=MODE_ENFORCE))
        task_id = "t1"
        for _ in range(3):
            guard.record_turn(task_id=task_id, agent_name="lisa", error_class="FileNotFoundError")
        assert guard.is_halted(task_id)

        guard.record_human_message(task_id=task_id, agent_name="lisa")
        assert not guard.is_halted(task_id)

        trip = guard.record_turn(task_id=task_id, agent_name="lisa", error_class=None)
        assert trip is None
        assert not guard.is_halted(task_id)

    def test_rescued_error_streak_trip_does_not_re_halt_with_differing_error(self):
        # Rescue from error_streak; next turn carries a different error class
        # (not enough for a new streak yet) — must NOT re-halt.
        guard = StuckGuard(GuardConfig(turn_cap=100, error_streak_threshold=3, mode=MODE_ENFORCE))
        task_id = "t1"
        for _ in range(3):
            guard.record_turn(task_id=task_id, agent_name="lisa", error_class="TimeoutError")
        assert guard.is_halted(task_id)

        guard.record_human_message(task_id=task_id, agent_name="lisa")
        assert not guard.is_halted(task_id)

        trip = guard.record_turn(task_id=task_id, agent_name="lisa", error_class="ValueError")
        assert trip is None
        assert not guard.is_halted(task_id)

    def test_manual_kill_remains_sticky_after_rescue(self):
        # record_human_message must NOT clear a TRIP_MANUAL_KILL halt.
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE))
        guard.kill(task_id="t1", agent_name="lisa")
        assert guard.is_halted("t1")

        guard.record_human_message(task_id="t1", agent_name="lisa")

        assert guard.is_halted("t1")
        state = guard.get_state("t1")
        assert state.halt_reason is not None
        assert state.halt_reason.kind == TRIP_MANUAL_KILL
