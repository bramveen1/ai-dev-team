"""Regression test for #897 — epic-lane worker feed silent on Slack.

Drives the real supervision entry point (``check_dispatch`` →
``feed_transport.post``, ``router/dispatch/supervision.py:412``) for a
worker dispatched via the epic lane and asserts its PR-ready terminal line
reaches the Slack ChatAdapter with the epic's own ``ConversationRef`` —
not ``slack_post`` posting to ``OPERATOR_DM`` with a garbage ``thread_ts``
(the pre-fix defect). Also asserts a discord-lane worker and a
manual-slack-lane worker are unaffected (AC3).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import feed_transport, supervision
from router.dispatch import state as dstate

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


@pytest.fixture(autouse=True)
def reset_delta_cache():
    supervision.reset_delta_cache()
    yield
    supervision.reset_delta_cache()


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


def _seed_dispatch(
    root: str,
    *,
    dispatch_id: str = "disp-epic-1",
    channel: str = "",
    thread_ts: str = "",
    agent: str = "sam",
) -> None:
    dstate.write_field(dispatch_id, dstate.FIELD_PID, str(os.getpid()), root=root)
    dstate.write_field(
        dispatch_id,
        dstate.FIELD_STARTED_AT,
        datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc).isoformat(),
        root=root,
    )
    dstate.write_field(dispatch_id, dstate.FIELD_BUDGET, "1800", root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_CHANNEL, channel, root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_THREAD_TS, thread_ts, root=root)
    dstate.write_field(dispatch_id, dstate.FIELD_AGENT, agent, root=root)


def _payload(
    *,
    dispatch_id: str = "disp-epic-1",
    channel: str = "",
    thread_ts: str = "",
    agent: str = "sam",
    transport: str = "",
    conversation_id: str = "",
) -> dict:
    payload = {
        "dispatch_id": dispatch_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "agent": agent,
    }
    if transport:
        payload["transport"] = transport
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return payload


def _no_op_gh(monkeypatch):
    """PR-ready path shells out to `gh pr ready` — stub it, not under test here."""
    monkeypatch.setattr(
        supervision.subprocess,
        "run",
        lambda cmd, **kw: MagicMock(returncode=0, stderr=b""),
    )


@pytest.mark.asyncio
class TestEpicLaneSlackAdapterRouting:
    """AC1/AC2: epic lane, EPIC_STATUS_TRANSPORT=slack, SLACK_VIA_ADAPTER=1,
    DISPATCH_FEED_VIA_CHAT_ADAPTER=1 — a worker's PR-ready line must reach
    the Slack ChatAdapter using the epic's own conversation_ref, and must
    never touch the (channel-less) Slack client directly.
    """

    def _enable(self, monkeypatch):
        monkeypatch.setattr(feed_transport, "is_enabled", lambda: True)
        monkeypatch.setattr(feed_transport, "_slack_via_adapter_enabled", lambda: True)

    def _slack_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.send_message = AsyncMock()
        return adapter

    async def test_pr_ready_terminal_line_routes_through_slack_adapter(self, root, slack_client, monkeypatch):
        self._enable(monkeypatch)
        adapter = self._slack_adapter()
        monkeypatch.setattr(feed_transport.runtime, "slack_adapter_for_agent", lambda agent: adapter)
        _no_op_gh(monkeypatch)

        epic_ref = "slack:C0BDN96EE75:1700000000.000100"
        _seed_dispatch(root)
        dstate.write_field("disp-epic-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-epic-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(transport="slack", conversation_id=epic_ref),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 0}
        # The PR-ready line reaches the Slack ChatAdapter with the epic's ref...
        adapter.send_message.assert_awaited_once()
        outbound = adapter.send_message.await_args.args[0]
        assert str(outbound.conversation_ref) == epic_ref
        assert "/pull/9" in outbound.text
        # ...and never falls back to slack_post/OPERATOR_DM with a garbage thread_ts.
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_slack_via_adapter_off_falls_back_to_legacy_slack_post(self, root, slack_client, monkeypatch):
        """SLACK_VIA_ADAPTER off: even with a real epic conversation_id
        persisted, "slack" never joins the effective adapter set — degrades
        to the historical channel/thread_ts slack_post call."""
        monkeypatch.setattr(feed_transport, "is_enabled", lambda: True)
        monkeypatch.setattr(feed_transport, "_slack_via_adapter_enabled", lambda: False)
        adapter = self._slack_adapter()
        monkeypatch.setattr(feed_transport.runtime, "slack_adapter_for_agent", lambda agent: adapter)
        _no_op_gh(monkeypatch)

        _seed_dispatch(root, channel="C_FALLBACK", thread_ts="1.0")
        dstate.write_field("disp-epic-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-epic-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(
                channel="C_FALLBACK",
                thread_ts="1.0",
                transport="slack",
                conversation_id="slack:C0BDN96EE75:1700000000.000100",
            ),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 0}
        adapter.send_message.assert_not_awaited()
        slack_client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
class TestNoRegression:
    """AC3: discord-lane and manual-slack-lane workers are unaffected."""

    async def test_discord_lane_worker_still_routes_through_discord_adapter(self, root, slack_client, monkeypatch):
        monkeypatch.setattr(feed_transport, "is_enabled", lambda: True)
        monkeypatch.setattr(feed_transport, "_slack_via_adapter_enabled", lambda: True)
        discord_adapter = MagicMock()
        discord_adapter.send_message = AsyncMock()
        slack_adapter_lookup = MagicMock(side_effect=AssertionError("slack adapter must not be resolved"))
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: discord_adapter)
        monkeypatch.setattr(feed_transport.runtime, "slack_adapter_for_agent", slack_adapter_lookup)
        _no_op_gh(monkeypatch)

        _seed_dispatch(root, dispatch_id="disp-discord-1")
        dstate.write_field("disp-discord-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-discord-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(
                dispatch_id="disp-discord-1",
                transport="discord",
                conversation_id="discord:1:2:3",
            ),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 0}
        discord_adapter.send_message.assert_awaited_once()
        outbound = discord_adapter.send_message.await_args.args[0]
        assert str(outbound.conversation_ref) == "discord:1:2:3"
        slack_client.chat_postMessage.assert_not_awaited()

    async def test_manual_slack_lane_worker_still_uses_legacy_slack_post(self, root, slack_client, monkeypatch):
        """A manual/legacy Slack dispatch persists transport="slack" (the
        sidecar default) but no conversation_id — even with
        SLACK_VIA_ADAPTER on, that must still degrade to legacy slack_post
        against the supplied channel/thread_ts, never a log-and-skip."""
        monkeypatch.setattr(feed_transport, "is_enabled", lambda: True)
        monkeypatch.setattr(feed_transport, "_slack_via_adapter_enabled", lambda: True)
        slack_adapter_lookup = MagicMock(side_effect=AssertionError("slack adapter must not be resolved"))
        monkeypatch.setattr(feed_transport.runtime, "slack_adapter_for_agent", slack_adapter_lookup)
        _no_op_gh(monkeypatch)

        _seed_dispatch(root, dispatch_id="disp-manual-1", channel="C_MANUAL", thread_ts="42.0")
        dstate.write_field("disp-manual-1", dstate.FIELD_EXITCODE, "0", root=root)
        dstate.write_field("disp-manual-1", dstate.FIELD_PR_URL, "https://github.com/o/r/pull/9", root=root)

        result = await supervision.check_dispatch(
            payload=_payload(
                dispatch_id="disp-manual-1",
                channel="C_MANUAL",
                thread_ts="42.0",
                transport="slack",
                conversation_id="",
            ),
            slack_client=slack_client,
            dispatch_root=root,
        )

        assert result == {"status": "done", "reason": "exitcode", "exitcode": 0}
        slack_client.chat_postMessage.assert_awaited_once()
        assert slack_client.chat_postMessage.call_args.kwargs["channel"] == "C_MANUAL"
