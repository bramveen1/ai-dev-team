"""Unit tests for router.dispatch.drain.

All tests use ``tmp_path`` for the dispatch workspace; no test touches
``/var/lib/dispatch/``.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from router.dispatch import drain as drain_mod
from router.dispatch import state as dstate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_dispatch(
    root: str,
    *,
    dispatch_id: str = "disp-1",
    agent: str = "sam",
    exitcode: str | None = None,
    heartbeat: bool = True,
) -> Path:
    """Write a minimal dispatch dir under *root*."""
    import os

    d = Path(root) / dispatch_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent").write_text(agent)
    (d / "started_at").write_text("2026-01-01T00:00:00Z")
    if heartbeat:
        hb = d / "heartbeat"
        hb.write_text("")
        now = time.time()
        os.utime(hb, (now, now))
    if exitcode is not None:
        (d / "exitcode").write_text(exitcode)
    return d


def _make_step_clock(values: list[float]):
    """Return a clock function that pops values in order (last value repeats)."""
    idx = 0

    def clock():
        nonlocal idx
        v = values[idx]
        if idx < len(values) - 1:
            idx += 1
        return v

    return clock


# ---------------------------------------------------------------------------
# In-flight detection
# ---------------------------------------------------------------------------


class TestInflightDetection:
    def test_no_exitcode_fresh_heartbeat_is_inflight(self, tmp_path):
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d1", agent="sam")
        result = drain_mod._inflight_for_agent("sam", root_str=root)
        assert result == ["d1"]

    def test_exitcode_present_is_not_inflight(self, tmp_path):
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d2", agent="sam", exitcode="0")
        result = drain_mod._inflight_for_agent("sam", root_str=root)
        assert result == []

    def test_stale_heartbeat_is_not_inflight(self, tmp_path):
        import os

        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d3", agent="sam")
        hb = tmp_path / "d3" / "heartbeat"
        ancient = time.time() - 3600
        os.utime(hb, (ancient, ancient))
        result = drain_mod._inflight_for_agent("sam", root_str=root)
        assert result == []

    def test_missing_heartbeat_is_not_inflight(self, tmp_path):
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d4", agent="sam", heartbeat=False)
        result = drain_mod._inflight_for_agent("sam", root_str=root)
        assert result == []

    def test_different_agent_not_counted(self, tmp_path):
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d5", agent="lisa")
        result = drain_mod._inflight_for_agent("sam", root_str=root)
        assert result == []


# ---------------------------------------------------------------------------
# Agent discovery / docker compose parsing
# ---------------------------------------------------------------------------


class TestAgentDiscovery:
    def test_running_agents_filters_known_agents(self):
        """_running_agents intersects docker output with KNOWN_AGENTS."""
        fake_output = "sam\nlisa\nrouter\nbrowser-use\nunknown-svc\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_output
            mock_run.return_value.returncode = 0
            agents = drain_mod._running_agents()
        assert agents == {"sam", "lisa"}

    def test_running_agents_excludes_router_and_browser_use(self):
        fake_output = "router\nbrowser-use\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_output
            result = drain_mod._running_agents()
        assert result == set()

    def test_running_agents_returns_empty_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("docker not found")):
            agents = drain_mod._running_agents()
        assert agents == set()


# ---------------------------------------------------------------------------
# Parallel polling — completes when last dispatch exits
# ---------------------------------------------------------------------------


class TestParallelPoll:
    @pytest.mark.asyncio
    async def test_drain_completes_when_last_dispatch_exits(self, tmp_path):
        """Drain returns 0 as soon as the last in-flight dispatch clears."""
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d-sam", agent="sam")
        _seed_dispatch(root, dispatch_id="d-lisa", agent="lisa")

        def fake_agents():
            return {"sam", "lisa"}

        call_count = 0
        original_collect = drain_mod._collect_inflight

        def patched_collect(agents, *, root_str):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Simulate both dispatches finishing.
                (tmp_path / "d-sam" / "exitcode").write_text("0")
                (tmp_path / "d-lisa" / "exitcode").write_text("0")
                return {a: [] for a in agents}
            return original_collect(agents, root_str=root_str)

        # Clock: always returns 0 so timeout never fires.
        clock = _make_step_clock([0.0])

        with (
            patch.object(drain_mod, "_collect_inflight", side_effect=patched_collect),
            patch.object(drain_mod, "_post_slack_sync"),
        ):
            exit_code = await drain_mod.drain(
                "abc123",
                timeout_seconds=1800,
                _running_agents_fn=fake_agents,
                _clock=clock,
                _poll_interval=0,
            )

        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_drain_polls_both_agents_concurrently(self, tmp_path):
        """_collect_inflight is called with all agents at once, not sequentially."""
        _seed_dispatch(str(tmp_path), dispatch_id="d-sam", agent="sam")
        _seed_dispatch(str(tmp_path), dispatch_id="d-lisa", agent="lisa")

        seen_agents: list[set] = []

        def recording_collect(agents, *, root_str):
            seen_agents.append(set(agents))
            # Return empty immediately so drain exits cleanly.
            return {a: [] for a in agents}

        def fake_agents():
            return {"sam", "lisa"}

        clock = _make_step_clock([0.0])

        with (
            patch.object(drain_mod, "_collect_inflight", side_effect=recording_collect),
            patch.object(drain_mod, "_post_slack_sync"),
        ):
            exit_code = await drain_mod.drain(
                "abc123",
                timeout_seconds=1800,
                _running_agents_fn=fake_agents,
                _clock=clock,
                _poll_interval=0,
            )

        assert exit_code == 0
        # Both agents must appear in a single collect call.
        assert any({"sam", "lisa"} <= s for s in seen_agents)


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_fires_and_returns_exit_1(self, tmp_path):
        """When timeout elapses, drain returns 1."""
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d-sam", agent="sam")

        def fake_agents():
            return {"sam"}

        slack_posts: list[str] = []

        def capture_slack(url, text):
            slack_posts.append(text)

        # Clock: start=0, first elapsed check=0, after sleep=10 (> timeout of 5).
        clock = _make_step_clock([0.0, 0.0, 10.0])

        with (
            patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "http://fake"}),
            patch.object(drain_mod, "_post_slack_sync", side_effect=capture_slack),
        ):
            exit_code = await drain_mod.drain(
                "deadbeef",
                timeout_seconds=5,
                root_path=tmp_path,
                _running_agents_fn=fake_agents,
                _clock=clock,
                _poll_interval=0,
            )

        assert exit_code == 1
        assert any(":warning:" in msg for msg in slack_posts)

    @pytest.mark.asyncio
    async def test_timeout_zero_skips_drain_immediately(self, tmp_path):
        """--timeout 0 returns exit 0 without touching the filesystem."""
        reads: list[str] = []
        original_list = dstate.list_dispatch_ids

        def recording_list(**kwargs):
            reads.append("list_dispatch_ids called")
            return original_list(**kwargs)

        with patch.object(dstate, "list_dispatch_ids", side_effect=recording_list):
            exit_code = await drain_mod.drain("abc123", timeout_seconds=0, root_path=tmp_path)

        assert exit_code == 0
        assert reads == [], "filesystem must not be touched when --timeout 0"


# ---------------------------------------------------------------------------
# Slack message formatting
# ---------------------------------------------------------------------------


class TestSlackMessages:
    @pytest.mark.asyncio
    async def test_start_message_format(self, tmp_path):
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d-sam", agent="sam")

        def fake_agents():
            return {"sam"}

        posts: list[str] = []

        def capture(url, text):
            posts.append(text)

        call_count = 0
        original_collect = drain_mod._collect_inflight

        def patched_collect(agents, *, root_str):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return {a: [] for a in agents}
            return original_collect(agents, root_str=root_str)

        clock = _make_step_clock([0.0])

        with (
            patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "http://fake"}),
            patch.object(drain_mod, "_post_slack_sync", side_effect=capture),
            patch.object(drain_mod, "_collect_inflight", side_effect=patched_collect),
        ):
            await drain_mod.drain(
                "abc1234567",
                timeout_seconds=1800,
                _running_agents_fn=fake_agents,
                _clock=clock,
                _poll_interval=0,
            )

        assert posts, "expected at least one Slack post"
        start_msg = posts[0]
        assert ":hourglass_flowing_sand:" in start_msg
        assert "abc1234567" in start_msg
        assert "sam:" in start_msg
        assert "1 in-flight" in start_msg

    @pytest.mark.asyncio
    async def test_no_slack_posted_when_zero_inflight_at_start(self, tmp_path):
        """When N=0 at t=0, no Slack message is posted."""
        posts: list[str] = []

        def capture(url, text):
            posts.append(text)

        def fake_agents():
            return {"sam"}

        clock = _make_step_clock([0.0])

        with (
            patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "http://fake"}),
            patch.object(drain_mod, "_post_slack_sync", side_effect=capture),
        ):
            exit_code = await drain_mod.drain(
                "abc123",
                timeout_seconds=1800,
                root_path=tmp_path,
                _running_agents_fn=fake_agents,
                _clock=clock,
                _poll_interval=0,
            )

        assert exit_code == 0
        assert posts == []

    @pytest.mark.asyncio
    async def test_missing_webhook_does_not_block(self, tmp_path):
        """Missing SLACK_WEBHOOK_URL: drain continues without error."""
        root = str(tmp_path)
        _seed_dispatch(root, dispatch_id="d-sam", agent="sam")

        def fake_agents():
            return {"sam"}

        call_count = 0
        original_collect = drain_mod._collect_inflight

        def patched_collect(agents, *, root_str):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return {a: [] for a in agents}
            return original_collect(agents, root_str=root_str)

        clock = _make_step_clock([0.0])

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(drain_mod, "_collect_inflight", side_effect=patched_collect),
        ):
            exit_code = await drain_mod.drain(
                "abc123",
                timeout_seconds=1800,
                _running_agents_fn=fake_agents,
                _clock=clock,
                _poll_interval=0,
            )

        assert exit_code == 0
