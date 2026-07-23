"""Tick orchestration for the autonomous bug-backlog loop.

This module owns the per-tick decision tree and the verdict + labelling
step; the mechanics it composes live in the sibling modules (``state``,
``triage``, ``github``, ``inflight``, ``worker``, ``notify``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from router import settings
from router.auto_dispatch.config import (
    AUTO_MERGE_LABEL,
    AWAITING_MAX_AGE_SECONDS,
    CALLABLE_REF,
    DEFAULT_COUNTER_PATH,
    DEFAULT_PERIOD_SECONDS,
    MERGE_PAT_PATH,
    PENDING_APPROVAL_MAX_AGE_SECONDS,
    TASK_NAME,
    load_auto_dispatch_config,
)
from router.auto_dispatch.github import (
    _apply_auto_merge_label,
    _ci_green,
    _get_issue,
    _get_pr_details,
    _get_pr_files,
    _get_pr_for_issue,
    _get_verdict_from_pr,
    _read_pat,
    _resolve_approvers,
    _TokenError,
    pick_next_candidate,
)
from router.auto_dispatch.inflight import (
    _get_in_flight_issue_nums,
    _has_any_in_flight_dispatch,
    _run_periodic_orphan_sweep,
)
from router.auto_dispatch.notify import _slack_post, _slack_post_with_ts
from router.auto_dispatch.state import (
    _add_awaiting,
    _add_pending_approval,
    _awaiting_path,
    _pending_approval_path,
    _read_awaiting,
    _read_last_stall_state,
    _read_pending_approval,
    _remove_awaiting,
    _stall_state_path,
    _write_awaiting,
    _write_last_stall_state,
    get_counters,
    increment_counters,
)
from router.auto_dispatch.triage import _pre_dispatch_triage, triage
from router.auto_dispatch.worker import _dispatch_worker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Awaiting driver — drives dispatched issues through verdict + labelling
# ---------------------------------------------------------------------------


async def _process_awaiting(
    *,
    repo: str,
    pat: str,
    slack_client: Any,
    destination: str | None,
    cfg: dict,
    payload: dict,
    now_ts: float,
) -> None:
    """Drive every awaited (already-dispatched) issue through ``handle_pr_verdict``.

    Runs every tick, independent of the new-dispatch rate caps — finishing
    in-flight work is not rate-limited. For each awaited issue:

    * find its open PR (``_get_pr_for_issue``);
    * ``None`` → check the issue itself (``_get_issue``): a *closed* issue
      means its PR already merged (or it was closed by hand) — terminal,
      remove now rather than waiting out the age-out. Otherwise the worker
      just hasn't opened a PR yet (keep until ``AWAITING_MAX_AGE_SECONDS``,
      then expire);
    * otherwise call ``handle_pr_verdict``: ``pending`` (no verdict yet) keeps
      the entry for the next tick; any other status is terminal for the tracker
      (labeled / would_label = handed to the merge queue; hold / fail / error =
      a human owns it now).
    """
    awaiting_path = _awaiting_path(payload)
    awaiting = _read_awaiting(awaiting_path)
    if not awaiting:
        return

    pat_path = payload.get("pat_path", MERGE_PAT_PATH)
    counter_path = payload.get("counter_path", DEFAULT_COUNTER_PATH)

    for issue_str, enqueued_ts in list(awaiting.items()):
        try:
            issue_num = int(issue_str)
        except (TypeError, ValueError):
            # Corrupt key — drop it so it can't wedge the loop.
            data = _read_awaiting(awaiting_path)
            data.pop(issue_str, None)
            _write_awaiting(awaiting_path, data)
            continue

        pr = await _get_pr_for_issue(repo, issue_num, pat)
        if pr is None:
            issue = await _get_issue(repo, issue_num, pat)
            if issue is not None and issue.get("state") == "closed":
                logger.info(
                    "auto_dispatch: issue #%s is closed with no open PR (merged/closed elsewhere); "
                    "dropping from tracker",
                    issue_num,
                )
                _remove_awaiting(awaiting_path, issue_num)
                continue
            try:
                age = now_ts - float(enqueued_ts)
            except (TypeError, ValueError):
                age = AWAITING_MAX_AGE_SECONDS + 1
            if age > AWAITING_MAX_AGE_SECONDS:
                logger.warning(
                    "auto_dispatch: issue #%s awaited > %ss with no open PR; dropping from tracker",
                    issue_num,
                    AWAITING_MAX_AGE_SECONDS,
                )
                _remove_awaiting(awaiting_path, issue_num)
            else:
                logger.info("auto_dispatch: issue #%s has no open PR yet; will recheck next tick", issue_num)
            continue

        pr_num = pr["number"]
        outcome = await handle_pr_verdict(
            repo=repo,
            pr_num=pr_num,
            issue_num=issue_num,
            slack_client=slack_client,
            destination=destination,
            pat_path=pat_path,
            shadow_mode=cfg["shadow_mode"],
            counter_path=counter_path,
            now=now_ts,
        )
        status = outcome.get("status")
        if status == "pending":
            # Verdict not posted yet — keep awaiting, recheck next tick.
            continue
        # Everything else is terminal for the tracker. (CI-not-green is treated
        # as terminal/hold-for-human by design: by the time a verdict exists CI
        # is normally complete, and biasing to a human on red is the safe side.)
        _remove_awaiting(awaiting_path, issue_num)
        logger.info(
            "auto_dispatch: issue #%s PR #%s reached terminal status=%s reason=%s",
            issue_num,
            pr_num,
            status,
            outcome.get("reason"),
        )


def _pending_approval_is_fresh(ts: Any, now_ts: float) -> bool:
    """Return True if a pending-approval timestamp is numeric and within the TTL.

    ``ts`` comes straight from the on-disk sidecar (``_read_pending_approval`` /
    ``state._read_json``), which only guarantees the root is a dict — not that
    values are numeric. A non-numeric or missing value is treated as expired
    (mirrors ``thread_loader._ts_key`` / ``merge_queue``'s defensive-float pattern)
    rather than raising and wedging the tick.
    """
    try:
        return (now_ts - float(ts)) < PENDING_APPROVAL_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Main tick callable (invoked by the scheduler system task)
# ---------------------------------------------------------------------------


async def tick(*, payload: dict, slack_client: Any, now: datetime) -> dict:
    """Self-bounded entry point invoked by the scheduler system task.

    #502: the scheduler awaits system tasks INLINE with no timeout, so a slow
    tick (serial GitHub IO) would freeze ``run_once`` and every other due task.
    The general fix belongs in the scheduler and is tracked in #502; until then
    we defend the loop here by capping the whole tick at ``tick_timeout`` seconds.
    Always returns ``{"status": "ok", ...}`` — the task is permanent.
    """
    timeout = payload.get("tick_timeout", 120)
    try:
        return await asyncio.wait_for(
            _tick_impl(payload=payload, slack_client=slack_client, now=now),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("auto_dispatch: tick exceeded %ss budget; aborting this cycle to protect the scheduler", timeout)
        return {"status": "ok", "skipped": "tick_timeout"}


async def _tick_impl(*, payload: dict, slack_client: Any, now: datetime) -> dict:
    """Auto-dispatch system-task callable, invoked by the scheduler every period.

    Always returns ``{"status": "ok"}`` — the task is permanent (never deregisters).

    Decision tree per tick:

    1. Read config; bail if disabled.
    2. Check rate/daily caps.
    3. Assert no in-flight dispatch (one-in-flight gate).
    4. Fetch open PRs; assert merge queue empty and zero open dev PRs.
    5. Pick next eligible candidate issue.
    6. Triage (path-glob + file-count); hold if sensitive.
    7. Dispatch worker against the issue's AC block.
    8. (After worker PR lands) get verdict + CI.
    9. Shadow mode → "would label"; live mode → apply ``auto-merge`` label
       (merge_queue.py performs the actual merge on a later tick).
    """
    repo: str = payload.get("repo", "")
    pat_path: str = payload.get("pat_path", MERGE_PAT_PATH)
    # Resolved per tick so an AUTO_DISPATCH_CHANNEL change in the runtime
    # config applies on the next tick without re-registering the task or
    # recreating the container (#576). The payload value (baked in at
    # registration) is only a fallback.
    destination: str | None = (
        settings.get("AUTO_DISPATCH_CHANNEL")
        or payload.get("destination")
        or settings.get("OPERATOR_DM_CHANNEL")
        or None
    )
    counter_path: str = payload.get("counter_path", DEFAULT_COUNTER_PATH)
    config_path: str | None = payload.get("config_path")

    if not repo:
        logger.error("auto_dispatch: payload missing 'repo'; skipping tick")
        return {"status": "ok", "skipped": "no_repo"}

    # 1. Config — read fresh every tick so changes take effect without restart.
    cfg = load_auto_dispatch_config(config_path)

    if not cfg["enabled"]:
        logger.debug("auto_dispatch: disabled (auto_dispatch.enabled=false)")
        return {"status": "ok", "skipped": "disabled"}

    now_ts = now.timestamp()

    # 1c. Periodic orphan age-out — runs every tick, best-effort, so _orphans/
    # stays bounded without requiring a container restart.
    _run_periodic_orphan_sweep()

    # 1b. Drive already-dispatched issues through verdict + labelling BEFORE the
    # new-dispatch caps — finishing in-flight work is not rate-limited. Reading
    # the PAT here is best-effort: if it's unavailable we skip awaiting
    # processing and fall through to the (capped) new-dispatch path below.
    try:
        _awaiting_pat = _read_pat(pat_path)
    except _TokenError:
        _awaiting_pat = None
    if _awaiting_pat is not None:
        try:
            await _process_awaiting(
                repo=repo,
                pat=_awaiting_pat,
                slack_client=slack_client,
                destination=destination,
                cfg=cfg,
                payload=payload,
                now_ts=now_ts,
            )
        except (_TokenError, httpx.HTTPError) as exc:
            logger.error("auto_dispatch: awaiting processing error: %s", exc)

    counters = get_counters(counter_path, now_ts)

    # 2a. Daily cap.
    if counters["daily_count"] >= cfg["daily_cap"]:
        logger.info("auto_dispatch: daily cap reached (%d/%d)", counters["daily_count"], cfg["daily_cap"])
        return {"status": "ok", "skipped": "daily_cap"}

    # 2b. Hourly rate.
    if counters["hourly_count"] >= cfg["rate_per_hour"]:
        logger.info("auto_dispatch: hourly rate reached (%d/%d)", counters["hourly_count"], cfg["rate_per_hour"])
        return {"status": "ok", "skipped": "hourly_rate"}

    # 3. One-in-flight gate.
    if _has_any_in_flight_dispatch():
        logger.info("auto_dispatch: dispatch already in flight; suppressing")
        return {"status": "ok", "skipped": "in_flight"}

    # Read PAT — fail loud.
    try:
        pat = _read_pat(pat_path)
    except _TokenError as exc:
        msg = f":x: auto-dispatch: {exc}"
        logger.error("auto_dispatch: %s", exc)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "skipped": "token_error"}

    # 4. Pick next eligible bug. Exclude live dispatches (no exitcode yet),
    # awaiting-tracker issues (PR open, verdict pending), AND issues with a
    # live un-acted approval card (#566) so the same card is never re-posted
    # on every tick during the human-decision window.
    in_flight_nums = _get_in_flight_issue_nums()
    in_flight_nums |= {int(k) for k in _read_awaiting(_awaiting_path(payload)) if str(k).isdigit()}
    _pending = _read_pending_approval(_pending_approval_path(payload))
    in_flight_nums |= {
        int(k) for k, ts in _pending.items() if str(k).isdigit() and _pending_approval_is_fresh(ts, now_ts)
    }
    try:
        candidate, skip_summary = await pick_next_candidate(repo, pat, in_flight_issue_nums=in_flight_nums)
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch: error picking candidate: %s", exc)
        return {"status": "ok", "skipped": "candidate_error"}

    if candidate is None:
        total_bugs = skip_summary["total_bugs"]
        skip_counts = skip_summary["skip_counts"]
        if total_bugs > 0:
            logger.warning(
                "auto_dispatch: %d bug(s) in queue, 0 dispatch-ready (skip_counts=%s)",
                total_bugs,
                skip_counts,
            )
            stall_path = _stall_state_path(payload)
            last_state = _read_last_stall_state(stall_path)
            if last_state != skip_summary:
                skip_parts = ", ".join(
                    f"{count} {reason.replace('_', '-')}" for reason, count in sorted(skip_counts.items())
                )
                msg = f":warning: auto-dispatch: {total_bugs} bug(s) in queue, 0 dispatch-ready — skipped: {skip_parts}"
                await _slack_post(slack_client, destination, msg)
                _write_last_stall_state(stall_path, skip_summary)
            else:
                logger.info("auto_dispatch: stall notification suppressed (skip state unchanged)")
        else:
            logger.info("auto_dispatch: no open bug issues found")
        return {"status": "ok", "skipped": "no_candidate"}

    issue_num = candidate["number"]
    issue_title = candidate.get("title", f"issue #{issue_num}")
    issue_url = candidate.get("html_url", f"https://github.com/{repo}/issues/{issue_num}")

    # 6. Triage: we don't have a PR diff yet (no PR exists), so we triage
    # based on the issue's title/labels as a best-effort pre-dispatch gate.
    # If the issue body hints at sensitive areas we hold early; otherwise
    # we let the post-dispatch triage (on the actual PR files) make the call.
    # The actual diff-based triage runs after the worker PR lands (step 8).
    # Pre-dispatch: conservatively check issue title/body for deny keywords.
    triage_decision, triage_reason = _pre_dispatch_triage(candidate)

    if triage_decision == "hold":
        msg = (
            f":warning: auto-dispatch: issue #{issue_num} pre-dispatch triage → *hold* "
            f"(reason: {triage_reason}) — {issue_url}"
        )
        logger.info("auto_dispatch: issue #%s triage=hold reason=%s", issue_num, triage_reason)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "action": "hold", "issue": issue_num, "reason": triage_reason}

    # 7. Shadow mode gate — never spawn a real worker in shadow mode.
    if cfg["shadow_mode"]:
        msg = (
            f":ghost: auto-dispatch (shadow): would dispatch worker for issue #{issue_num} "
            f"({issue_title}) — {issue_url}"
        )
        await _slack_post(slack_client, destination, msg)
        logger.info(
            "auto_dispatch: shadow mode — would dispatch worker for issue #%s",
            issue_num,
        )
        return {"status": "ok", "action": "would_dispatch", "issue": issue_num}

    # 7b. Post a kickoff message to anchor the autonomous dispatch to a Slack thread.
    # The autonomous path has no originating thread; we create one here so the
    # dispatch handler receives a valid thread_ts instead of an empty string.
    kickoff_ts = await _slack_post_with_ts(
        slack_client,
        destination,
        f":gear: auto-dispatch: starting worker for issue #{issue_num} ({issue_title}) — {issue_url}",
    )

    # 7b-guard: no Slack channel / client → kickoff_ts is empty.  Proceeding
    # would cause the handler to fail with missing_slack_context.  Bail out
    # deterministically (#563 negative-path requirement).
    if not kickoff_ts:
        logger.error(
            "auto_dispatch: empty kickoff_ts for issue #%s (no Slack channel or post failed); "
            "skipping dispatch to avoid missing_slack_context on the handler leg",
            issue_num,
        )
        return {"status": "ok", "skipped": "empty_slack_context", "issue": issue_num}

    # 7c. Dispatch worker.
    logger.info("auto_dispatch: dispatching worker for issue #%s (%s)", issue_num, issue_title)
    worker_outcome: str = ""
    try:
        worker_outcome = await asyncio.wait_for(
            _dispatch_worker(
                issue_url=issue_url,
                issue_num=issue_num,
                issue_title=issue_title,
                slack_client=slack_client,
                destination=destination,
                thread_ts=kickoff_ts,
                payload=payload,
            ),
            timeout=payload.get("dispatch_timeout", 60),
        )
    except asyncio.TimeoutError:
        logger.warning("auto_dispatch: worker dispatch timed out for issue #%s", issue_num)
        return {"status": "ok", "skipped": "dispatch_timeout"}
    except Exception:
        logger.exception("auto_dispatch: worker dispatch error for issue #%s", issue_num)
        return {"status": "ok", "skipped": "dispatch_error"}

    # approval_required: gate fired, draft posted, human must click Approve.
    # Do NOT increment counters or enrol in awaiting — no worker was spawned.
    # DO record in pending-approval so subsequent ticks skip this issue until
    # the card is acted on or the TTL expires (#566 dedup fix).
    if worker_outcome == "approval_required":
        _add_pending_approval(_pending_approval_path(payload), issue_num, now_ts)
        return {"status": "ok", "action": "approval_required", "issue": issue_num}

    # Increment counters only after a successful dispatch kick-off.
    increment_counters(counter_path, now_ts)

    # Enrol the issue in the awaiting tracker so a *later* tick drives it
    # through verdict + labelling via ``_process_awaiting``. Without this the
    # whole verdict bridge never fires — the dispatch would land a PR that no
    # tick ever picks up. Persisted (atomic) so a restart doesn't lose it.
    _add_awaiting(_awaiting_path(payload), issue_num, now_ts)

    # 8. The verdict + labelling step is driven on a *subsequent* tick by
    # ``_process_awaiting`` (step 1b above): once the worker opens a PR and a
    # verdict lands, ``handle_pr_verdict`` runs the diff-based triage + gated
    # ``auto-merge`` labelling, then removes the issue from the awaiting set
    # (the merge queue owns it from there). This tick is done.
    return {"status": "ok", "action": "dispatched", "issue": issue_num}


# ---------------------------------------------------------------------------
# Verdict + post-dispatch labelling decision
# ---------------------------------------------------------------------------


async def handle_pr_verdict(
    *,
    repo: str,
    pr_num: int,
    issue_num: int,
    slack_client: Any,
    destination: str | None,
    pat_path: str = MERGE_PAT_PATH,
    shadow_mode: bool = True,
    counter_path: str = DEFAULT_COUNTER_PATH,
    now: float | None = None,
) -> dict:
    """Called (e.g. by supervision) after a worker PR is created.

    Runs the diff-based triage, reads the Sam verdict, checks CI, and on the
    safe path applies the ``auto-merge`` label (live mode) or posts "would
    label" (shadow mode). The actual merge is owned by merge_queue.py.
    """
    try:
        pat = _read_pat(pat_path)
    except _TokenError as exc:
        logger.error("auto_dispatch.handle_pr_verdict: %s", exc)
        return {"status": "error", "reason": "token_error"}

    # Diff-based triage on the real PR files.
    try:
        changed_files = await _get_pr_files(repo, pr_num, pat)
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch.handle_pr_verdict: error fetching PR files: %s", exc)
        return {"status": "error", "reason": "pr_files_error"}

    triage_decision, triage_reason = triage(changed_files)
    logger.info(
        "auto_dispatch.handle_pr_verdict: PR #%s triage=%s reason=%s files=%s",
        pr_num,
        triage_decision,
        triage_reason,
        changed_files,
    )

    pr_url = f"https://github.com/{repo}/pull/{pr_num}"

    if triage_decision == "hold":
        msg = f":warning: auto-dispatch: PR #{pr_num} → *hold for human* (triage reason: {triage_reason}) — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": triage_reason}

    # Get the review verdict from a configured approver.
    try:
        verdict = await _get_verdict_from_pr(repo, pr_num, pat, _resolve_approvers())
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch.handle_pr_verdict: error reading verdict: %s", exc)
        return {"status": "error", "reason": "verdict_error"}

    if verdict is None:
        logger.info("auto_dispatch.handle_pr_verdict: no verdict yet for PR #%s", pr_num)
        return {"status": "pending", "reason": "no_verdict"}

    if verdict != "pass":
        msg = f":x: auto-dispatch: PR #{pr_num} verdict=*fail* → hold for human — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": "verdict_fail"}

    # Check CI.
    try:
        pr_details = await _get_pr_details(repo, pr_num, pat)
        head_sha = (pr_details.get("head") or {}).get("sha", "")
        ci_ok = await _ci_green(repo, head_sha, pat) if head_sha else False
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch.handle_pr_verdict: CI check error: %s", exc)
        return {"status": "error", "reason": "ci_check_error"}

    if not ci_ok:
        msg = f":x: auto-dispatch: PR #{pr_num} CI not green → hold for human — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": "ci_not_green"}

    # All gates passed → hand the PR to the merge queue (do NOT merge here).
    if shadow_mode:
        msg = (
            f":ghost: auto-dispatch (shadow): would label PR #{pr_num} "
            f"`{AUTO_MERGE_LABEL}` (issue #{issue_num}) — triage=low_risk, verdict=pass, CI=green — {pr_url}"
        )
        await _slack_post(slack_client, destination, msg)
        logger.info(
            "auto_dispatch: shadow mode — would label PR #%s issue #%s %s",
            pr_num,
            issue_num,
            AUTO_MERGE_LABEL,
        )
        return {"status": "would_label", "pr": pr_num, "issue": issue_num}

    # Live mode: apply the auto-merge label; merge_queue.py merges on a later tick.
    try:
        labelled = await _apply_auto_merge_label(repo, pr_num, pat)
    except _TokenError as exc:
        logger.error("auto_dispatch.handle_pr_verdict: label PAT error: %s", exc)
        await _slack_post(slack_client, destination, f":x: auto-dispatch: labelling failed ({exc}) — {pr_url}")
        return {"status": "error", "reason": "label_token_error"}
    except httpx.HTTPError as exc:
        logger.error("auto_dispatch.handle_pr_verdict: HTTP error applying label: %s", exc)
        return {"status": "error", "reason": "label_http_error"}

    if not labelled:
        msg = f":x: auto-dispatch: failed to label PR #{pr_num} `{AUTO_MERGE_LABEL}` — hold for human — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": "label_failed"}

    msg = (
        f":label: auto-dispatch: labelled PR #{pr_num} `{AUTO_MERGE_LABEL}` "
        f"(issue #{issue_num}) → merge queue will merge when green — {pr_url}"
    )
    await _slack_post(slack_client, destination, msg)
    logger.info("auto_dispatch: labelled PR #%s issue #%s %s → merge queue", pr_num, issue_num, AUTO_MERGE_LABEL)
    return {"status": "labeled", "pr": pr_num, "issue": issue_num}


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def register_auto_dispatch(
    store: Any,
    *,
    agent_name: str,
    repo: str,
    pat_path: str = MERGE_PAT_PATH,
    destination: str | None = None,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
    counter_path: str = DEFAULT_COUNTER_PATH,
) -> Any:
    """Idempotently register the auto-dispatch system task in *store*.

    Safe to call on every router boot — if the task already exists under
    :data:`CALLABLE_REF` it is returned untouched rather than duplicated.
    """
    existing = store.list_by_callable_ref(CALLABLE_REF)
    if existing:
        task = existing[0]
        current = dict(task.payload)
        desired: dict[str, Any] = {
            "repo": repo,
            "pat_path": pat_path,
            "counter_path": counter_path,
        }
        if destination:
            desired["destination"] = destination
        changed = {k: (current.get(k), v) for k, v in desired.items() if current.get(k) != v}
        if changed:
            merged = {**current, **desired}
            store.update_payload(task.task_id, merged)
            for key, (old_val, new_val) in changed.items():
                logger.info(
                    "auto_dispatch: reconciled payload key=%s old=%r new=%r task_id=%s",
                    key,
                    old_val,
                    new_val,
                    task.task_id,
                )
            task = store.get(task.task_id)
        else:
            logger.debug("auto_dispatch: system task already registered (%s)", task.task_id)
        return task

    payload: dict[str, Any] = {
        "repo": repo,
        "pat_path": pat_path,
        "counter_path": counter_path,
    }
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
        "auto_dispatch: registered system task task_id=%s repo=%s period=%ss agent=%s",
        task.task_id,
        repo,
        period_seconds,
        agent_name,
    )
    return task
