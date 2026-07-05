"""Integration tests for the stuck-guard hooks in router.dispatcher."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import router.dispatcher as dispatcher_mod
from router.dispatcher import TaskHaltedError, _handle_guard_trip, dispatch
from router.stuck_guard import (
    MODE_DRY_RUN,
    MODE_ENFORCE,
    TRIP_TURN_CAP,
    GuardConfig,
    GuardTrip,
    StuckGuard,
    make_task_id,
)

pytestmark = pytest.mark.unit


_MOCK_CLI_STDOUT = json.dumps(
    {
        "result": "ok",
        "session_id": "test-session-00000000",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 12, "output_tokens": 5},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
                ],
            }
        ],
    }
)


@pytest.fixture(autouse=True)
def mock_thread_loader():
    with patch("router.dispatcher.load_thread_history", new_callable=AsyncMock) as mock:
        mock.return_value = []
        yield mock


@pytest.fixture
def mock_container():
    with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
        mock.return_value = (_MOCK_CLI_STDOUT, "", 0)
        yield mock


class TestDispatcherGuardHooks:
    @pytest.mark.asyncio
    async def test_successful_dispatch_records_turn(self, mock_slack_client, mock_container, tmp_path):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN, post_mortem_dir=str(tmp_path)))
        await dispatch(
            agent_name="lisa",
            message="hello",
            channel="C1",
            thread_ts="1.0",
            client=mock_slack_client,
            guard=guard,
        )
        state = guard.get_state(make_task_id("C1", "1.0", "lisa"))
        assert state is not None
        assert len(state.turns) == 1
        assert state.turns[0].error_class is None
        assert state.turns[0].tool_name == "bash"

    @pytest.mark.asyncio
    async def test_loop_trip_in_dry_run_does_not_block(self, mock_slack_client, mock_container, tmp_path):
        # Acceptance: "In dry-run mode, agent continues after a trip; Slack
        # message tagged [DRY-RUN]."
        guard = StuckGuard(
            GuardConfig(
                mode=MODE_DRY_RUN,
                turn_cap=100,
                loop_window=5,
                loop_threshold=3,
                post_mortem_dir=str(tmp_path),
            )
        )
        for _ in range(3):
            await dispatch(
                agent_name="lisa",
                message="run ls",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )
        # All three calls succeeded (no TaskHaltedError raised).
        state = guard.get_state(make_task_id("C1", "1.0", "lisa"))
        assert state is not None
        assert len(state.turns) == 3
        assert not guard.is_halted(make_task_id("C1", "1.0", "lisa"))

    @pytest.mark.asyncio
    async def test_loop_trip_in_enforce_blocks_next_dispatch(self, mock_slack_client, mock_container, tmp_path):
        # Acceptance: "In enforce mode, agent halts after a trip; subsequent
        # dispatch attempts are rejected until a fresh task is started."
        guard = StuckGuard(
            GuardConfig(
                mode=MODE_ENFORCE,
                turn_cap=100,
                loop_window=5,
                loop_threshold=3,
                post_mortem_dir=str(tmp_path),
            )
        )
        # Three identical bash(ls) calls trips the loop guard on the third.
        for _ in range(3):
            await dispatch(
                agent_name="lisa",
                message="run ls",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )
        assert guard.is_halted(make_task_id("C1", "1.0", "lisa"))

        # Fourth dispatch must now be rejected.
        with pytest.raises(TaskHaltedError):
            await dispatch(
                agent_name="lisa",
                message="another",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )

    @pytest.mark.asyncio
    async def test_kill_blocks_subsequent_dispatch_in_enforce(self, mock_slack_client, mock_container, tmp_path):
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE, post_mortem_dir=str(tmp_path)))
        guard.kill(task_id=make_task_id("C1", "1.0", "lisa"), agent_name="lisa")

        with pytest.raises(TaskHaltedError):
            await dispatch(
                agent_name="lisa",
                message="hi",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )

    @pytest.mark.asyncio
    async def test_kill_blocks_subsequent_dispatch_in_dry_run(self, mock_slack_client, mock_container, tmp_path):
        # Acceptance: "/kill ... honored regardless of guard mode." Even
        # though dry-run never halts on a *guard trip*, a manual kill is
        # the human override and must always stop the agent.
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN, post_mortem_dir=str(tmp_path)))
        guard.kill(task_id=make_task_id("C1", "1.0", "lisa"), agent_name="lisa")

        with pytest.raises(TaskHaltedError):
            await dispatch(
                agent_name="lisa",
                message="hi",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )

    @pytest.mark.asyncio
    async def test_dry_run_does_not_block_on_guard_trip(self, mock_slack_client, mock_container, tmp_path):
        # Counterpart: a regular guard trip in dry-run *does not* halt —
        # only `/kill` does. That's the whole point of dry-run.
        guard = StuckGuard(
            GuardConfig(
                mode=MODE_DRY_RUN,
                turn_cap=2,
                post_mortem_dir=str(tmp_path),
            )
        )
        # Two dispatches → 2nd hits the cap and trips, but dry-run keeps going.
        await dispatch(
            agent_name="lisa",
            message="hi",
            channel="C1",
            thread_ts="1.0",
            client=mock_slack_client,
            guard=guard,
        )
        await dispatch(
            agent_name="lisa",
            message="hi again",
            channel="C1",
            thread_ts="1.0",
            client=mock_slack_client,
            guard=guard,
        )
        # Third dispatch must still go through — guard tripped but did not halt.
        await dispatch(
            agent_name="lisa",
            message="hi once more",
            channel="C1",
            thread_ts="1.0",
            client=mock_slack_client,
            guard=guard,
        )

    @pytest.mark.asyncio
    async def test_cli_failure_records_error_class(self, mock_slack_client, tmp_path):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN, post_mortem_dir=str(tmp_path)))
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "boom", 1)
            with pytest.raises(Exception):
                await dispatch(
                    agent_name="lisa",
                    message="hi",
                    channel="C1",
                    thread_ts="1.0",
                    client=mock_slack_client,
                    guard=guard,
                )
        state = guard.get_state(make_task_id("C1", "1.0", "lisa"))
        assert state is not None
        assert state.turns[-1].error_class is not None
        assert "NonZeroExit" in state.turns[-1].error_class

    @pytest.mark.asyncio
    async def test_three_consecutive_cli_failures_trip_error_streak(self, mock_slack_client, tmp_path):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN, error_streak_threshold=3, post_mortem_dir=str(tmp_path)))
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "", 1)
            for _ in range(3):
                with pytest.raises(Exception):
                    await dispatch(
                        agent_name="lisa",
                        message="hi",
                        channel="C1",
                        thread_ts="1.0",
                        client=mock_slack_client,
                        guard=guard,
                    )

        state = guard.get_state(make_task_id("C1", "1.0", "lisa"))
        assert state is not None
        # Streak should have tripped on the 3rd identical error.
        assert state.halt_reason is not None
        assert state.halt_reason.kind == "error_streak"


class TestHumanInitiatedReset:
    """#422: a human-initiated dispatch resets the turn cap and clears a
    guard-tripped halt, so a healthy human-steered thread never bricks at the
    cap in enforce mode. Manual /kill halts stay sticky."""

    @pytest.mark.asyncio
    async def test_human_initiated_dispatch_resets_turn_cap(self, mock_slack_client, mock_container, tmp_path):
        # turn_cap=2 → the 2nd automated turn trips and halts in enforce mode.
        # Loop guard disabled (threshold high) so we isolate turn_cap: the mock
        # CLI emits identical bash(ls) turns which would otherwise trip the loop
        # guard on stale history that record_human_message intentionally keeps.
        guard = StuckGuard(
            GuardConfig(
                mode=MODE_ENFORCE,
                turn_cap=2,
                loop_window=50,
                loop_threshold=50,
                post_mortem_dir=str(tmp_path),
            )
        )
        task_id = make_task_id("C1", "1.0", "lisa")
        for _ in range(2):
            await dispatch(
                agent_name="lisa",
                message="run ls",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )
        assert guard.is_halted(task_id)

        # A human re-engaging clears the halt and resets the counter, so the
        # dispatch is NOT rejected — this is the bug #422 fix.
        await dispatch(
            agent_name="lisa",
            message="hey, keep going",
            channel="C1",
            thread_ts="1.0",
            client=mock_slack_client,
            guard=guard,
            human_initiated=True,
        )
        assert not guard.is_halted(task_id)
        # Counter was reset before the new turn was recorded: only the single
        # post-reset turn remains counted.
        state = guard.get_state(task_id)
        assert state is not None
        assert state.turn_count == 1

    @pytest.mark.asyncio
    async def test_non_human_dispatch_does_not_reset(self, mock_slack_client, mock_container, tmp_path):
        # Default (human_initiated=False) preserves a guard-tripped halt: a
        # bot handoff must not rescue a tripped thread.
        guard = StuckGuard(
            GuardConfig(
                mode=MODE_ENFORCE,
                turn_cap=2,
                loop_window=50,
                loop_threshold=50,
                post_mortem_dir=str(tmp_path),
            )
        )
        task_id = make_task_id("C1", "1.0", "lisa")
        for _ in range(2):
            await dispatch(
                agent_name="lisa",
                message="run ls",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )
        assert guard.is_halted(task_id)

        with pytest.raises(TaskHaltedError):
            await dispatch(
                agent_name="lisa",
                message="auto handoff",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
            )

    @pytest.mark.asyncio
    async def test_human_initiated_does_not_clear_manual_kill(self, mock_slack_client, mock_container, tmp_path):
        # /kill is the human override and stays sticky even against a later
        # human message — only reset_task unwedges it.
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE, post_mortem_dir=str(tmp_path)))
        task_id = make_task_id("C1", "1.0", "lisa")
        guard.kill(task_id=task_id, agent_name="lisa")

        with pytest.raises(TaskHaltedError):
            await dispatch(
                agent_name="lisa",
                message="let me back in",
                channel="C1",
                thread_ts="1.0",
                client=mock_slack_client,
                guard=guard,
                human_initiated=True,
            )
        assert guard.is_halted(task_id)


class TestStuckGuardNotificationTracking:
    """Verify the notification task is strongly referenced until it completes.

    Without the fix the bare ensure_future result was discarded; asyncio can
    GC weak-referenced tasks mid-flight so the Slack post would silently drop.
    """

    @pytest.mark.asyncio
    async def test_notification_task_tracked_until_complete(self, mock_slack_client, tmp_path):
        trip = GuardTrip(kind="turn_cap", threshold=10, observed=10, description="Turn cap reached")

        mock_state = MagicMock()
        mock_guard = MagicMock()
        mock_guard.get_state.return_value = mock_state
        mock_guard.config = MagicMock()

        # Isolate the module-level set for this test.
        saved = set(dispatcher_mod._background_tasks)
        dispatcher_mod._background_tasks.clear()
        try:
            with (
                patch("router.dispatcher.write_post_mortem", return_value=None),
                patch("router.dispatcher.format_slack_message", return_value="alert text"),
            ):
                _handle_guard_trip(
                    guard=mock_guard,
                    trip=trip,
                    task_id="C1:1.0:lisa",
                    agent_name="lisa",
                    channel="C1",
                    thread_ts="1.0",
                    client=mock_slack_client,
                    task_description=None,
                )

            # Task must be strongly held before it runs.
            assert len(dispatcher_mod._background_tasks) == 1, (
                "notification task not tracked — GC can drop it before it posts"
            )

            # Capture the task, then let the event loop run it to completion.
            (task,) = dispatcher_mod._background_tasks
            await task

            # Done-callback must have removed it from the set.
            assert len(dispatcher_mod._background_tasks) == 0, (
                "done-callback did not discard the task from _background_tasks"
            )
        finally:
            dispatcher_mod._background_tasks.clear()
            dispatcher_mod._background_tasks.update(saved)


class TestDryRunNotificationDedup:
    """Regression tests for issue #687: dry-run Slack spam suppression.

    A stuck scheduled lane that trips turn_cap on every tick must not post
    to Slack on every tick — only the first trip for a (task_id, trip_kind)
    pair should produce a notification.
    """

    @pytest.mark.asyncio
    async def test_dry_run_trip_posts_once_not_on_repeat(self, mock_slack_client, tmp_path):
        guard = StuckGuard(GuardConfig(mode=MODE_DRY_RUN, turn_cap=2, post_mortem_dir=str(tmp_path)))
        task_id = "C1:1.0:lisa"

        # Seed state so get_state() returns something for the trip handler.
        trip = GuardTrip(kind=TRIP_TURN_CAP, threshold=2, observed=2, description="test trip")
        guard.record_turn(task_id=task_id, agent_name="lisa", timestamp=1_000_000.0)

        saved = set(dispatcher_mod._background_tasks)
        dispatcher_mod._background_tasks.clear()
        try:
            with (
                patch("router.dispatcher.write_post_mortem", return_value=tmp_path / "pm.md"),
                patch("router.dispatcher.format_slack_message", return_value="alert"),
            ):
                # First trip → should notify.
                _handle_guard_trip(
                    guard=guard,
                    trip=trip,
                    task_id=task_id,
                    agent_name="lisa",
                    channel="C1",
                    thread_ts="1.0",
                    client=mock_slack_client,
                    task_description=None,
                )
                tasks_after_first = set(dispatcher_mod._background_tasks)
                for t in tasks_after_first:
                    await t
                dispatcher_mod._background_tasks.clear()

                # Second trip with same (task_id, kind) → must be suppressed.
                _handle_guard_trip(
                    guard=guard,
                    trip=trip,
                    task_id=task_id,
                    agent_name="lisa",
                    channel="C1",
                    thread_ts="1.0",
                    client=mock_slack_client,
                    task_description=None,
                )
                tasks_after_second = set(dispatcher_mod._background_tasks)
                for t in tasks_after_second:
                    await t

        finally:
            dispatcher_mod._background_tasks.clear()
            dispatcher_mod._background_tasks.update(saved)

        # Slack must have been called exactly once (first trip only).
        assert mock_slack_client.chat_postMessage.call_count == 1, "Dry-run repeat trip must not re-post to Slack"

    @pytest.mark.asyncio
    async def test_enforce_mode_always_posts(self, mock_slack_client, tmp_path):
        # In enforce mode, every trip posts (halted task prevents further trips
        # in practice, but the notification path must not be gated by dedup).
        guard = StuckGuard(GuardConfig(mode=MODE_ENFORCE, post_mortem_dir=str(tmp_path)))
        task_id = "C1:1.0:lisa"
        trip = GuardTrip(kind=TRIP_TURN_CAP, threshold=2, observed=2, description="test trip")
        guard.record_turn(task_id=task_id, agent_name="lisa", timestamp=1_000_000.0)

        saved = set(dispatcher_mod._background_tasks)
        dispatcher_mod._background_tasks.clear()
        try:
            with (
                patch("router.dispatcher.write_post_mortem", return_value=tmp_path / "pm.md"),
                patch("router.dispatcher.format_slack_message", return_value="alert"),
            ):
                _handle_guard_trip(
                    guard=guard,
                    trip=trip,
                    task_id=task_id,
                    agent_name="lisa",
                    channel="C1",
                    thread_ts="1.0",
                    client=mock_slack_client,
                    task_description=None,
                )
                for t in set(dispatcher_mod._background_tasks):
                    await t
                dispatcher_mod._background_tasks.clear()

                _handle_guard_trip(
                    guard=guard,
                    trip=trip,
                    task_id=task_id,
                    agent_name="lisa",
                    channel="C1",
                    thread_ts="1.0",
                    client=mock_slack_client,
                    task_description=None,
                )
                for t in set(dispatcher_mod._background_tasks):
                    await t
        finally:
            dispatcher_mod._background_tasks.clear()
            dispatcher_mod._background_tasks.update(saved)

        assert mock_slack_client.chat_postMessage.call_count == 2, "Enforce mode must always post, never suppressed"
