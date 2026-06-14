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
