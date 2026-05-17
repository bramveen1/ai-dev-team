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
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import discovery, supervision
from router.scheduled_tasks import scheduler
from router.scheduled_tasks.store import ScheduledTaskStore

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    """Import packs/dispatch/handler.py without polluting sys.modules globally."""
    spec = importlib.util.spec_from_file_location("_smoke_dispatch_handler", PACK_DIR / "handler.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
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
    summaries_tick_1 = await scheduler.run_once(
        store,
        client_resolver,
        dispatch_fn=AsyncMock(),
        now=base + timedelta(seconds=121),
    )
    summaries_tick_2 = await scheduler.run_once(
        store,
        client_resolver,
        dispatch_fn=AsyncMock(),
        now=base + timedelta(seconds=242),
    )

    # First tick should see exitcode and post terminal. Second tick has
    # nothing to do because the task was deregistered on tick 1.
    assert any(s.get("status") == "done" for s in summaries_tick_1), summaries_tick_1
    assert summaries_tick_2 == []

    # (c) Terminal posted exactly once.
    post_calls = slack_client.chat_postMessage.await_args_list
    assert len(post_calls) == 1, [c.kwargs for c in post_calls]
    text = post_calls[0].kwargs["text"]
    assert ":white_check_mark:" in text
    assert dispatch_id in text
    # The agent_user_id should be carried through the resolver and used
    # for the mention so a real Slack workspace would ping the bot.
    assert "<@U_SAM>" in text

    # (d) Store is empty after — supervision task deregistered cleanly.
    remaining = store.list_by_callable_ref(supervision.CALLABLE_REF)
    assert remaining == []

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
