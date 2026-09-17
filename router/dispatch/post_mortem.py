"""Post-mortem request queue for worker timeout terminations (#899).

When a dispatched worker terminates on timeout/``budget_overrun`` (as
distinct from a hard-fail, #867), :mod:`router.dispatch.supervision`
enqueues a post-mortem request here instead of surfacing the raw failure
to a human. :func:`router.auto_dispatch.loop._tick_impl` drains the queue
behind the same #866 concurrency cap and #868 circuit breaker every new
worker dispatch already respects, and launches a Sam worker to analyse
the dead worker's workspace and recommend salvage vs re-dispatch.

The queue is a single JSON list persisted beside the dispatch root (same
atomic-write idiom as every other sidecar under this package). Idempotency
is enforced by ``dstate.FIELD_POST_MORTEM_FIRED``, a marker written in the
dead dispatch's own state dir — separate from the queue file so "already
enqueued" survives even if the queue entry is later drained.
"""

from __future__ import annotations

import logging
from pathlib import Path

from router.atomic_io import atomic_read_json, atomic_write_json
from router.dispatch import state as dstate

logger = logging.getLogger(__name__)

_QUEUE_FILENAME = "_post_mortem_queue.json"

# Sam analyses, it does not implement — a short budget keeps this a
# read-only diagnostic pass rather than another #897-style budget sink.
POST_MORTEM_BUDGET_SECONDS = 900
POST_MORTEM_AGENT = "sam"
POST_MORTEM_PERSONA = "postmortem"
POST_MORTEM_MODEL = "sonnet"


def queue_path(dispatch_root: str | None = None) -> Path:
    return dstate.dispatch_root(dispatch_root) / _QUEUE_FILENAME


def _read_store(dispatch_root: str | None) -> dict:
    # Keyed by dispatch_id (same dict-sidecar idiom as the auto_dispatch
    # hard-failed/awaiting trackers) — atomic_read_json requires a JSON
    # object root, not a bare list.
    return atomic_read_json(queue_path(dispatch_root), default={})


def _write_store(dispatch_root: str | None, data: dict) -> None:
    try:
        atomic_write_json(queue_path(dispatch_root), data)
    except OSError:
        logger.warning("post_mortem: failed to write queue at %s", queue_path(dispatch_root), exc_info=True)


def read_queue(dispatch_root: str | None = None) -> list[dict]:
    return list(_read_store(dispatch_root).values())


def enqueue(
    dispatch_id: str,
    *,
    issue_url: str,
    workspace_path: str,
    reason: str,
    elapsed_seconds: int | None,
    budget_seconds: int | None,
    channel: str,
    thread_ts: str,
    transport: str = "",
    conversation_id: str = "",
    dispatch_root: str | None = None,
) -> bool:
    """Enqueue a post-mortem request for *dispatch_id*, exactly once.

    Returns True the first time (request queued + marker written), False
    on every later call for the same ``dispatch_id`` (marker already
    present) — the idempotency guarantee the timeout branch relies on so a
    router restart or a repeated tick can never fire two post-mortems for
    the same terminated worker.
    """
    dispatch_dir = dstate.ensure_dispatch_dir(dispatch_id, root=dispatch_root)
    marker_path = dispatch_dir / dstate.FIELD_POST_MORTEM_FIRED
    if marker_path.exists():
        return False

    data = _read_store(dispatch_root)
    data[dispatch_id] = {
        "dispatch_id": dispatch_id,
        "issue_url": issue_url,
        "workspace_path": workspace_path,
        "reason": reason,
        "elapsed_seconds": elapsed_seconds,
        "budget_seconds": budget_seconds,
        "channel": channel,
        "thread_ts": thread_ts,
        "transport": transport,
        "conversation_id": conversation_id,
    }
    _write_store(dispatch_root, data)
    try:
        marker_path.touch()
    except OSError:
        logger.warning("post_mortem: failed to write marker for dispatch=%s", dispatch_id, exc_info=True)
    return True


def remove(dispatch_id: str, dispatch_root: str | None = None) -> None:
    """Remove *dispatch_id*'s request from the queue (after a successful launch)."""
    data = _read_store(dispatch_root)
    if dispatch_id in data:
        del data[dispatch_id]
        _write_store(dispatch_root, data)


def build_prompt(request: dict) -> str:
    """The analytical prompt Sam runs against the dead worker's workspace."""
    dispatch_id = request.get("dispatch_id", "")
    workspace_path = request.get("workspace_path", "")
    issue_url = request.get("issue_url") or "(no issue url on record)"
    reason = request.get("reason", "budget_overrun")
    elapsed = request.get("elapsed_seconds")
    budget = request.get("budget_seconds")
    return (
        f"Post-mortem analysis, persona=postmortem. Worker dispatch `{dispatch_id}` "
        f"terminated on {reason} (ran {elapsed}s against a {budget}s budget) working "
        f"issue {issue_url}. Its workspace is at `{workspace_path}` — inspect it. Do "
        f"NOT implement anything, commit, push, or open a PR.\n\n"
        f"Report:\n"
        f"1. Was work actually completed (diff present in the workspace, tests "
        f"green)? Point to the workspace path and what you found.\n"
        f"2. Root cause of the timeout: real code difficulty vs. process waste "
        f"(e.g. broad `pytest tests/unit`, repeated baseline diffs, "
        f"over-verification).\n"
        f"3. A lettered recommendation: (a) salvage — lint + commit + push the "
        f"intact diff, `Closes #N`, or (b) re-dispatch with a tightened AC/budget "
        f"— give the concrete AC delta.\n\n"
        f"You are analysis-only: do not commit, push, open a PR, or re-dispatch "
        f"anything yourself — a human or the merge daemon owns that decision. Do "
        f"not run the whole test suite or re-derive the diff from scratch; read "
        f"what's already there."
    )


def build_dispatch_cmd(request: dict, *, model: str = POST_MORTEM_MODEL) -> list[str]:
    """The docker-exec argv that launches the Sam post-mortem worker.

    Mirrors the ``dispatch_issue`` invocation :func:`router.auto_dispatch
    .worker._dispatch_worker` builds, but adds ``--exec`` (the handler's
    smoke-probe escape hatch — see ``packs/dispatch/handler.py``) so the
    worker runs the analytical prompt above instead of the standard
    "implement the issue and open a PR" one, and ``--approved`` since a
    read-only analysis pass carries none of the risk the approval gate
    exists to catch.
    """
    claude_cmd = [
        "claude",
        "-p",
        build_prompt(request),
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--add-dir",
        request.get("workspace_path", ""),
    ]
    return [
        "python",
        "/config/packs/dispatch/handler.py",
        "dispatch_issue",
        "--issue-url",
        request.get("issue_url") or "",
        "--channel",
        request.get("channel") or "",
        "--thread-ts",
        request.get("thread_ts") or "",
        "--agent",
        POST_MORTEM_AGENT,
        "--supervision-mode",
        "poll",
        "--persona",
        POST_MORTEM_PERSONA,
        "--model",
        model,
        "--budget-seconds",
        str(POST_MORTEM_BUDGET_SECONDS),
        "--summary",
        f"post-mortem: dispatch {request.get('dispatch_id', '')} {request.get('reason', 'timeout')}",
        "--approved",
        "--exec",
        *claude_cmd,
    ]
