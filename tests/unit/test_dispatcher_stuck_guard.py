"""Integration tests for the stuck-guard hooks in router.dispatcher."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from router.dispatcher import TaskHaltedError, dispatch
from router.stuck_guard import (
    MODE_DRY_RUN,
    MODE_ENFORCE,
    GuardConfig,
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
