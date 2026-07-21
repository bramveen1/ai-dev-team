"""Epic orchestrator tick — Stage 1 (#755) + Stage 2 auto-dispatch (#756).

Behind the ``EPIC_ORCHESTRATOR`` master flag (default off, hot-reload —
``router/settings.py``). For every epic configured in ``config/epic.yaml``:
walk its sub-issue DAG (:mod:`router.epic.dag`, #754), and for each child
that is *ready* (every parent PR merged to the base branch) but not yet
dispatched, either post a per-issue approval card (Stage 1) or, when the
``EPIC_AUTO_DISPATCH`` flag is also on (Stage 2), dispatch the worker
directly — no per-dispatch card. Approving a Stage 1 card, or a successful
Stage 2 auto-dispatch, both re-invoke the existing ``dispatch_issue``
handler exactly like a manual dispatch — worker → ``aidt-tl-sam`` review is
unchanged (#751's "reuses cleanly" list). Held children (an open parent PR)
are logged with reason ``parent_pr_open`` — the DAG gate the design's smoke
check looks for. A child whose own worker PR has merged (or whose issue
closed) is *terminal* and is never dispatched again, even after the
dispatched-tracker entry for it is cleared on landing (#768).

Stage 1 boundaries, enforced here by omission:

* No auto-launch. Every dispatch goes through a human-approved card, even
  on issues the bug loop's smart gate would launch unattended — this
  module never calls ``auto_dispatch``'s direct-launch path.
* No merge, no deploy. The only write this loop performs is applying the
  ``epic:<slug>`` label to a landed worker PR, which feeds #753's existing
  merge-gate exclusion — merging itself stays a human, hand-merge action.

Stage 2 (``EPIC_AUTO_DISPATCH``) narrows just the first bullet above: a
ready child is launched via ``router.auto_dispatch.worker._dispatch_worker``
(the same handler invocation the bug loop uses, ``--approved`` intentionally
omitted) instead of an unconditional card. That reuses the handler's own
smart gate — a card is still posted for the rare dispatch it flags. New
dispatches share the bug loop's existing dispatch caps (``config/dispatch.yaml``
``auto_dispatch.rate_per_hour`` / ``daily_cap``, and the handler's own
$50/5h quota check) — same counter file, so an epic auto-dispatch and a bug
auto-dispatch draw from one shared hourly/daily budget. No merge, no deploy
either way (unchanged from Stage 1).

Flag off → this module is inert: ``tick`` returns immediately, and it
touches no state the bug loop (``router.auto_dispatch``) reads or writes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from router import config, settings
from router.auto_dispatch.config import DEFAULT_COUNTER_PATH, load_auto_dispatch_config
from router.auto_dispatch.state import get_counters, increment_counters
from router.auto_dispatch.worker import _dispatch_worker
from router.epic.config import (
    CALLABLE_REF,
    DEFAULT_PERIOD_SECONDS,
    EPIC_PAT_PATH,
    TASK_NAME,
    load_epic_config,
)
from router.epic.dag import DagCycleError, DagError, build_dag, ready_nodes
from router.epic.github import (
    TokenError,
    _apply_epic_label,
    _get_issue,
    _get_open_pr_for_issue,
    _is_child_terminal,
)
from router.epic.state import _mark_dispatched, _read_dispatched, _remove_dispatched, _state_path
from router.github_api import read_pat
from router.slack_post import best_effort_post

logger = logging.getLogger(__name__)

# Hard ceiling on a single Stage-2 auto-dispatch attempt, mirroring the bug
# loop's own `payload.get("dispatch_timeout", 60)` wait_for around the same
# `_dispatch_worker` call (auto_dispatch/loop.py) — keeps a stuck container
# exec from stalling the whole tick.
_AUTO_DISPATCH_TIMEOUT_SECONDS = 60


def _epic_label(slug: str) -> str:
    return f"epic:{slug}"


async def _default_create_draft_fn(**kwargs: Any) -> None:
    """Default implementation: delegate to ``router.internal_api.create_dispatch_draft``."""
    from router.internal_api import create_dispatch_draft  # noqa: PLC0415

    await create_dispatch_draft(**kwargs)


async def _reconcile_landed_pr(
    *,
    repo: str,
    child: int,
    label: str,
    pr: dict,
    pat: str,
    state_path: str,
) -> None:
    """A PR already exists for *child* — apply the epic label if missing.

    Idempotent and never re-dispatches: once a PR is open the human review
    path (aidt-tl-sam) owns it. Clears the dispatched-tracker entry so the
    sidecar doesn't grow unbounded.
    """
    pr_num = pr["number"]
    label_names = {lbl.get("name") for lbl in pr.get("labels", [])}
    if label not in label_names:
        try:
            labelled = await _apply_epic_label(repo, pr_num, label, pat)
        except TokenError as exc:
            logger.error("epic_orchestrator: %s", exc)
            return
        if labelled:
            logger.info(
                "epic_orchestrator: labelled PR #%s `%s` (issue #%s)",
                pr_num,
                label,
                child,
            )
    _remove_dispatched(state_path, child)


async def _dispatch_ready_child(
    *,
    repo: str,
    epic_number: int,
    child: int,
    slug: str,
    cfg: dict,
    pat: str,
    state_path: str,
    counter_path: str,
    slack_client: Any,
    destination: str | None,
    now_ts: float,
    _create_draft_fn: Any = None,
) -> bool:
    """Post the kickoff line, then dispatch one ready sub-issue.

    Stage 1 (``EPIC_AUTO_DISPATCH`` off): posts an approval-card draft —
    unconditional, one per ready child.

    Stage 2 (``EPIC_AUTO_DISPATCH`` on): checks the bug loop's shared
    daily/hourly dispatch caps first (holding the child for a later tick if
    either is exhausted), then calls ``_dispatch_worker`` directly — the
    same handler invocation the bug loop uses. If the handler's own smart
    gate fires, a card is still posted (the handler's safety valve); the
    counters are only incremented on an actual worker launch.

    Returns True when the child was acted on this tick (card posted or
    worker launched); False when held (cap, error, or already pending).
    """
    dispatched = _read_dispatched(state_path)
    if str(child) in dispatched:
        logger.debug("epic_orchestrator: issue #%s already has a pending draft; skipping re-dispatch", child)
        return False

    auto_dispatch = bool(settings.get("EPIC_AUTO_DISPATCH"))
    if auto_dispatch:
        auto_cfg = load_auto_dispatch_config()
        counters = get_counters(counter_path, now_ts)
        if counters["daily_count"] >= auto_cfg["daily_cap"]:
            logger.info(
                "epic_orchestrator: auto-dispatch daily cap reached (%d/%d); holding issue #%s",
                counters["daily_count"],
                auto_cfg["daily_cap"],
                child,
            )
            return False
        if counters["hourly_count"] >= auto_cfg["rate_per_hour"]:
            logger.info(
                "epic_orchestrator: auto-dispatch hourly rate reached (%d/%d); holding issue #%s",
                counters["hourly_count"],
                auto_cfg["rate_per_hour"],
                child,
            )
            return False

    try:
        issue = await _get_issue(repo, child, pat)
    except TokenError as exc:
        logger.error("epic_orchestrator: %s", exc)
        return False
    if issue is None:
        logger.warning("epic_orchestrator: could not read issue #%s; skipping this tick", child)
        return False

    issue_title = issue.get("title") or f"issue #{child}"
    issue_url = issue.get("html_url") or f"https://github.com/{repo}/issues/{child}"

    kickoff_verb = "auto-dispatching" if auto_dispatch else "ready"
    kickoff_text = (
        f":gear: epic-orchestrator: epic #{epic_number} sub-issue #{child} {kickoff_verb} — {issue_title} — {issue_url}"
    )
    kickoff_ts = await best_effort_post(
        slack_client,
        destination,
        kickoff_text,
        log=logger,
        prefix="epic_orchestrator",
    )
    if not kickoff_ts:
        logger.error(
            "epic_orchestrator: empty kickoff_ts for issue #%s (no Slack channel or post failed); "
            "skipping dispatch this tick",
            child,
        )
        return False

    create_fn = _create_draft_fn or _default_create_draft_fn

    if auto_dispatch:
        try:
            outcome = await asyncio.wait_for(
                _dispatch_worker(
                    issue_url=issue_url,
                    issue_num=child,
                    issue_title=issue_title,
                    slack_client=slack_client,
                    destination=destination,
                    thread_ts=kickoff_ts,
                    payload=cfg,
                    _create_draft_fn=create_fn,
                ),
                timeout=_AUTO_DISPATCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("epic_orchestrator: auto-dispatch worker timed out for issue #%s", child)
            return False
        except Exception:
            logger.exception("epic_orchestrator: auto-dispatch worker error for issue #%s", child)
            return False

        if outcome == "approval_required":
            _mark_dispatched(state_path, child, slug, now_ts)
            logger.info(
                "epic_orchestrator: auto-dispatch gate fired for epic #%s sub-issue #%s; approval card posted",
                epic_number,
                child,
            )
            return True

        increment_counters(counter_path, now_ts)
        _mark_dispatched(state_path, child, slug, now_ts)
        logger.info(
            "epic_orchestrator: auto-dispatched epic #%s sub-issue #%s (%s)",
            epic_number,
            child,
            issue_title,
        )
        return True

    agent_name = cfg.get("worker_agent") or config.resolve_worker_agent()
    await create_fn(
        agent_name=agent_name,
        channel=destination or "",
        thread_ts=kickoff_ts,
        issue_url=issue_url,
        issue_num=child,
        issue_title=issue_title,
        model=cfg.get("worker_model", "sonnet"),
        persona=cfg.get("worker_persona", "dev"),
        budget_seconds=int(cfg.get("worker_budget_seconds", 1800)),
        gate_preview={"repo": repo, "gate_reason": f"epic_orchestrator:{epic_number}:{slug}"},
    )
    _mark_dispatched(state_path, child, slug, now_ts)
    logger.info(
        "epic_orchestrator: posted approval card for epic #%s sub-issue #%s (%s)",
        epic_number,
        child,
        issue_title,
    )
    return True


async def _process_ready_child(
    *,
    repo: str,
    epic_number: int,
    child: int,
    slug: str,
    cfg: dict,
    pat: str,
    state_path: str,
    counter_path: str,
    slack_client: Any,
    destination: str | None,
    now_ts: float,
    _create_draft_fn: Any = None,
) -> bool:
    """Handle one DAG-ready child: label a landed PR, or dispatch a fresh one."""
    label = _epic_label(slug)
    try:
        pr = await _get_open_pr_for_issue(repo, child, pat)
    except TokenError as exc:
        logger.error("epic_orchestrator: %s", exc)
        return False

    if pr is not None:
        await _reconcile_landed_pr(repo=repo, child=child, label=label, pr=pr, pat=pat, state_path=state_path)
        return False

    try:
        terminal = await _is_child_terminal(repo, child, pat)
    except TokenError as exc:
        logger.error("epic_orchestrator: %s", exc)
        return False
    if terminal:
        logger.debug("epic_orchestrator: issue #%s already terminal (closed issue / merged PR); no re-dispatch", child)
        return False

    return await _dispatch_ready_child(
        repo=repo,
        epic_number=epic_number,
        child=child,
        slug=slug,
        cfg=cfg,
        pat=pat,
        state_path=state_path,
        counter_path=counter_path,
        slack_client=slack_client,
        destination=destination,
        now_ts=now_ts,
        _create_draft_fn=_create_draft_fn,
    )


async def _process_epic(
    *,
    epic: dict,
    repo: str,
    base_branch: str,
    cfg: dict,
    pat: str,
    state_path: str,
    counter_path: str,
    slack_client: Any,
    destination: str | None,
    now_ts: float,
    _create_draft_fn: Any = None,
) -> dict:
    epic_number = epic.get("number")
    slug = epic.get("slug")
    if not epic_number or not slug:
        logger.warning("epic_orchestrator: skipping malformed epics entry %r (need number + slug)", epic)
        return {"dispatched": 0, "held": 0}

    try:
        dag = build_dag(epic_number, repo)
    except DagCycleError as exc:
        logger.error("epic_orchestrator: epic #%s has a dependency cycle; holding all children (%s)", epic_number, exc)
        await best_effort_post(
            slack_client,
            destination,
            f":x: epic-orchestrator: epic #{epic_number} has a dependency cycle — holding all children ({exc})",
            log=logger,
            prefix="epic_orchestrator",
        )
        return {"dispatched": 0, "held": 0}
    except DagError as exc:
        logger.error("epic_orchestrator: could not build DAG for epic #%s: %s", epic_number, exc)
        return {"dispatched": 0, "held": 0}

    ready = set(ready_nodes(dag, repo, base_branch=base_branch))
    held = sorted(set(dag) - ready)
    for child in held:
        logger.info(
            "epic_orchestrator: epic #%s child #%s held reason=parent_pr_open parents=%s",
            epic_number,
            child,
            dag[child],
        )

    dispatched_count = 0
    for child in sorted(ready):
        acted = await _process_ready_child(
            repo=repo,
            epic_number=epic_number,
            child=child,
            slug=slug,
            cfg=cfg,
            pat=pat,
            state_path=state_path,
            counter_path=counter_path,
            slack_client=slack_client,
            destination=destination,
            now_ts=now_ts,
            _create_draft_fn=_create_draft_fn,
        )
        if acted:
            dispatched_count += 1

    return {"dispatched": dispatched_count, "held": len(held)}


async def tick(*, payload: dict, slack_client: Any, now: datetime, _create_draft_fn: Any = None) -> dict:
    """Epic orchestrator system-task callable, invoked by the scheduler every period.

    Always returns ``{"status": "ok", ...}`` — the task is permanent
    (never deregisters), mirroring ``router.auto_dispatch.tick``.
    """
    if not settings.get("EPIC_ORCHESTRATOR"):
        logger.debug("epic_orchestrator: disabled (EPIC_ORCHESTRATOR flag off)")
        return {"status": "ok", "skipped": "disabled"}

    cfg = load_epic_config(payload.get("config_path"))
    repo: str = payload.get("repo") or cfg["repo"]
    epics: list[dict] = cfg["epics"]

    if not repo or not epics:
        logger.debug("epic_orchestrator: no repo/epics configured; skipping tick")
        return {"status": "ok", "skipped": "not_configured"}

    destination: str | None = payload.get("destination") or settings.get("OPERATOR_DM_CHANNEL") or None

    try:
        pat = read_pat(payload.get("pat_path", EPIC_PAT_PATH))
    except TokenError as exc:
        logger.error("epic_orchestrator: %s", exc)
        return {"status": "ok", "skipped": "token_error"}

    state_path = _state_path(payload)
    # Shared with the bug loop (router.auto_dispatch): Stage 2 auto-dispatch
    # draws from the *same* daily/hourly counter file, so epic and bug
    # dispatches together respect the one existing 12/day + 1/hr budget.
    counter_path = payload.get("counter_path", DEFAULT_COUNTER_PATH)
    now_ts = now.timestamp()

    dispatched_total = 0
    held_total = 0
    for epic in epics:
        result = await _process_epic(
            epic=epic,
            repo=repo,
            base_branch=cfg["base_branch"],
            cfg=cfg,
            pat=pat,
            state_path=state_path,
            counter_path=counter_path,
            slack_client=slack_client,
            destination=destination,
            now_ts=now_ts,
            _create_draft_fn=_create_draft_fn,
        )
        dispatched_total += result["dispatched"]
        held_total += result["held"]

    return {"status": "ok", "dispatched": dispatched_total, "held": held_total}


def register_epic_orchestrator(
    store: Any,
    *,
    agent_name: str,
    destination: str | None = None,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
) -> Any:
    """Idempotently register the epic orchestrator system task in *store*.

    Safe to call on every router boot — if the task already exists under
    :data:`CALLABLE_REF` it is returned untouched rather than duplicated.
    Content (which repo/epics to track) lives in ``config/epic.yaml`` and
    the ``EPIC_ORCHESTRATOR`` flag, both re-read every tick — no restart
    needed to add an epic or flip the switch.
    """
    existing = store.list_by_callable_ref(CALLABLE_REF)
    if existing:
        return existing[0]

    payload: dict[str, Any] = {}
    if destination:
        payload["destination"] = destination

    task = store.create_system_task(
        agent_name=agent_name,
        name=TASK_NAME,
        callable_ref=CALLABLE_REF,
        payload=payload,
        period_seconds=period_seconds,
    )
    logger.info(
        "epic_orchestrator: registered system task task_id=%s period=%ss agent=%s",
        task.task_id,
        period_seconds,
        agent_name,
    )
    return task
