"""End-to-end smoke probe for the dispatch supervision pipeline (#163).

Mirrors the smoke probe spelled out in the issue body:

    launch a 30s no-op dispatch (`sleep 30 && echo done > /var/lib/dispatch/<id>/exitcode`)
    and verify (a) handler returns in <2s, (b) scheduled task fires at
    least twice, (c) terminal message posted exactly once in the original
    thread, (d) deregister actually happens (check SQLite store is empty
    after).

This is what catches the "every piece works in isolation but the seams
are wrong" failure mode that the reviewer pointed out — register_supervision
existed but was never reachable from a real dispatch. We compress the
30s timer down to ~0.5s for CI by using a short sleep, but the wiring
exercised here (handler → babysit → state files → discovery → scheduler
→ supervisor → terminal post → deregister) is the same path a real
30-min dispatch follows.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.dispatch import discovery, supervision
from router.scheduled_tasks import scheduler
from router.scheduled_tasks.store import ScheduledTaskStore

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    """Import packs/dispatch/handler.py without polluting sys.modules globally.

    Installs a no-gate approval config so the smoke tests exercise the
    supervision wiring without tripping D-7's fail-closed default
    (``require_always: True``).  The approval-gate path itself is covered
    in ``tests/unit/packs/test_pack_dispatch_d7.py``.
    """
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_smoke_dispatch_handler", PACK_DIR / "handler.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._load_approval_config = lambda: {"require_always": False, "destructive_keywords": []}
    return module


@pytest.fixture
def dispatch_root(tmp_path, monkeypatch):
    root = tmp_path / "dispatches"
    root.mkdir()
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(root))
    return str(root)


@pytest.fixture
def store(tmp_path):
    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s
    s.close()


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.0"})
    return client


@pytest.fixture
def client_resolver(slack_client):
    return lambda _agent: slack_client


@pytest.fixture(autouse=True)
def reset_supervisor_cache():
    supervision.reset_delta_cache()
    yield
    supervision.reset_delta_cache()


def _reap_detached_babysits(handler) -> None:
    """Call .wait() on every parked Popen so pytest's resource-warning
    filter doesn't fire on the test-process exit. In production these
    are inherited by init; in-process tests have to do this ourselves.
    """
    for proc in handler._DETACHED_BABYSITS:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    handler._DETACHED_BABYSITS.clear()


@pytest.mark.asyncio
async def test_poll_mode_end_to_end(dispatch_root, store, slack_client, client_resolver, monkeypatch):
    """Issue's documented smoke probe, compressed to ~0.5s for CI.

    Drives the real wiring: handler → babysit → state files → discovery
    reconcile → scheduler tick → supervisor → terminal Slack post →
    task deregister. No mocks of router internals — the only seam is the
    Slack client.
    """
    handler = _load_handler()

    # (a) Handler returns in <2s — we want way under that, but assert
    # the AC explicitly so a regression that re-introduces blocking
    # would fail loud.
    start = time.monotonic()
    result = handler.dispatch_issue(
        issue_url="https://example.com/issues/1",
        channel="C1",
        thread_ts="1.0",
        agent="sam",
        budget_seconds=120,
        exec_override=["sleep", "0.3"],
        supervision_mode="poll",
    )
    handler_duration = time.monotonic() - start

    assert handler_duration < 2.0, f"handler took {handler_duration:.2f}s (>2s budget)"
    assert result["status"] == "launched", result
    dispatch_id = result["dispatch_id"]
    workspace = Path(result["workspace"])
    assert (workspace / "pid").exists(), "handler must persist pid before returning"

    # First discovery tick — registers supervision for the new dispatch.
    registered = discovery.reconcile_once(
        store,
        agent_user_id_resolver=lambda name: "U_SAM" if name == "sam" else None,
        period_seconds=120,
        root=dispatch_root,
    )
    assert registered == [dispatch_id]

    # Wait for the babysit to finish so its exitcode lands. The sleep
    # 0.3 above should be done well before the 5s deadline.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (workspace / "exitcode").exists():
            break
        await asyncio.sleep(0.05)
    assert (workspace / "exitcode").exists(), "babysit must write exitcode before terminal"
    assert (workspace / "exitcode").read_text().strip() == "0"

    # (b) Scheduler fires at least twice. We force the supervision task
    # due each call by passing now= forward of the row's next_run_at —
    # that's what the production scheduler does every poll_interval.
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc)
    await scheduler.run_once(
        store,
        client_resolver,
        dispatch_fn=AsyncMock(),
        now=base + timedelta(seconds=121),
    )
    summaries_tick_1 = await scheduler.drain_system_tasks()
    await scheduler.run_once(
        store,
        client_resolver,
        dispatch_fn=AsyncMock(),
        now=base + timedelta(seconds=242),
    )
    summaries_tick_2 = await scheduler.drain_system_tasks()

    # First tick should see exitcode and post terminal. Second tick has
    # nothing to do because the task was deregistered on tick 1.
    assert any(s.get("status") == "done" for s in summaries_tick_1), summaries_tick_1
    assert summaries_tick_2 == []

    # (c) Terminal posted exactly once, through the injected client (the only
    # seam). Issue #270 moved the workers-bot identity choice to the scheduler's
    # system_client_resolver; this test passes only client_resolver, so the
    # supervisor posts through that mock — supervision never builds its own
    # client (a regression that did so made a real Slack call and broke #273).
    post_calls = slack_client.chat_postMessage.await_args_list
    assert len(post_calls) == 1, [c.kwargs for c in post_calls]
    text = post_calls[0].kwargs["text"]
    assert ":white_check_mark:" in text
    # Issue #333: terminal/completion lines now identify the dispatch by its
    # human-readable issue label (#NNN from issue_url) rather than the raw
    # dispatch_id. The seeded issue_url is .../issues/1, so expect "#1".
    # (dispatch_id still appears on the launch line via the handler's
    # "[dispatch-id · persona]" bracket prefix, preserving in-thread correlation.)
    assert "#1" in text
    assert dispatch_id not in text
    # Issue #270: the terminal summary no longer @-mentions the agent — it
    # speaks as a runtime status line, not an agent-to-self ping.
    assert "<@" not in text

    # (d) Store is empty after — supervision task deregistered cleanly.
    remaining = store.list_by_callable_ref(supervision.CALLABLE_REF)
    assert remaining == []

    _reap_detached_babysits(handler)


@pytest.mark.asyncio
async def test_poll_mode_backfills_when_discovery_missed_the_launch(
    dispatch_root, store, slack_client, client_resolver
):
    """Issue #705: discovery being down for a dispatch's entire lifetime
    must not lose its terminal report.

    Unlike ``test_poll_mode_end_to_end``, this test deliberately skips the
    launch-time discovery tick — simulating the discovery loop being dead
    (or the router being down) for the whole run — and only reconciles
    once the dispatch is already terminal. The backfill path in
    ``reconcile_once`` must still register supervision so the next
    scheduler tick posts the terminal message, exactly as #705 requires.
    """
    handler = _load_handler()

    result = handler.dispatch_issue(
        issue_url="https://example.com/issues/2",
        channel="C1",
        thread_ts="1.0",
        agent="sam",
        budget_seconds=120,
        exec_override=["sleep", "0.3"],
        supervision_mode="poll",
    )
    assert result["status"] == "launched", result
    dispatch_id = result["dispatch_id"]
    workspace = Path(result["workspace"])

    # No discovery tick here — this is the whole point of the test: the
    # dispatch runs to completion with nobody ever having registered it.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (workspace / "exitcode").exists():
            break
        await asyncio.sleep(0.05)
    assert (workspace / "exitcode").exists(), "babysit must write exitcode before terminal"

    # First discovery reconcile happens only now, after the dispatch is
    # already terminal — this is the backfill case.
    registered = discovery.reconcile_once(
        store,
        agent_user_id_resolver=lambda name: "U_SAM" if name == "sam" else None,
        period_seconds=120,
        root=dispatch_root,
    )
    assert registered == [dispatch_id], registered

    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc)
    await scheduler.run_once(
        store,
        client_resolver,
        dispatch_fn=AsyncMock(),
        now=base + timedelta(seconds=121),
    )
    summaries = await scheduler.drain_system_tasks()
    assert any(s.get("status") == "done" for s in summaries), summaries

    # The terminal message still made it to Slack despite discovery having
    # missed the entire launch→completion window.
    post_calls = slack_client.chat_postMessage.await_args_list
    assert len(post_calls) == 1, [c.kwargs for c in post_calls]
    assert ":white_check_mark:" in post_calls[0].kwargs["text"]

    # And it deregistered cleanly, same as the normal path.
    assert store.list_by_callable_ref(supervision.CALLABLE_REF) == []

    # A second reconcile must not re-backfill (terminal_posted marker).
    again = discovery.reconcile_once(store, root=dispatch_root)
    assert again == []

    _reap_detached_babysits(handler)


@pytest.mark.asyncio
async def test_inline_mode_does_not_create_supervision_task(dispatch_root, store, slack_client, client_resolver):
    """Default inline mode must not register supervision — it blocks inline."""
    handler = _load_handler()

    result = handler.dispatch_issue(
        issue_url="https://example.com/issues/1",
        channel="C1",
        thread_ts="1.0",
        agent="sam",
        exec_override=["sleep", "0.1"],
        supervision_mode="inline",
    )

    assert result["status"] == "completed"
    assert result["exitcode"] == 0

    # Discovery still runs in production; it should skip the dispatch
    # because exitcode is already written (terminal state).
    registered = discovery.reconcile_once(store, root=dispatch_root)
    assert registered == []

    # And no supervision task was created.
    assert store.list_by_callable_ref(supervision.CALLABLE_REF) == []


@pytest.mark.asyncio
async def test_handler_output_is_valid_json(dispatch_root):
    """The handler's CLI prints a single line of JSON callers can parse."""
    handler = _load_handler()

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://example.com/i",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--agent",
                "sam",
                "--supervision-mode",
                "poll",
                "--exec",
                "sleep",
                "0.05",
            ]
        )
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["status"] == "launched"
    assert payload["supervision_mode"] == "poll"

    _reap_detached_babysits(handler)


def _load_babysit():
    """Import packs/dispatch/babysit.py without polluting sys.modules globally."""
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_smoke_dispatch_babysit", PACK_DIR / "babysit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_babysit_fires_80pct_warning_on_cost_event(tmp_path, monkeypatch):
    """Confirm babysit wires maybe_post_warning: warning posts when rolling window crosses 80%.

    Exercises the seam between the babysit watch loop and quota.maybe_post_warning so
    the D-5 80% AC is covered end-to-end, not just in unit isolation.
    """
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))

    # Pre-seed a completed dispatch whose cost is 75% of the $50 threshold.
    prior = tmp_path / "dispatch-prior"
    prior.mkdir()
    (prior / "started_at").write_text((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    (prior / "cost").write_text("37.50")  # 75% of $50

    # Set up the new dispatch workspace with Slack context.
    dispatch_id = "dispatch-test-warn"
    d = tmp_path / dispatch_id
    d.mkdir()
    (d / "started_at").write_text(datetime.now(timezone.utc).isoformat())
    (d / "channel").write_text("C_TEST")
    (d / "thread_ts").write_text("9.9")

    babysit = _load_babysit()

    # Craft a stream-json event that updates cost to $5 more (total window = $42.50 = 85%).
    cost_event = (
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 5.00,
                "is_error": False,
                "result": "done",
            }
        )
        + "\n"
    )

    posted: list = []

    def fake_slack_post(channel, thread_ts, text):
        posted.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        return True

    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO(cost_event)

    with patch.object(babysit, "_slack_post", side_effect=fake_slack_post):
        babysit._watch(fake_proc, dispatch_id)

    assert len(posted) == 1, f"expected 1 warning post, got {len(posted)}: {posted}"
    assert ":warning:" in posted[0]["text"]
    assert posted[0]["channel"] == "C_TEST"
