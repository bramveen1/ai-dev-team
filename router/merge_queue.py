"""Idle auto-merge scheduled task — squash-merges the oldest approved+green PR
when the system has been idle for 10 minutes (issue #437).

Implements a hand-rolled single-PR merge queue:

* One merge per tick (15-min cadence → ≤ 4 merges/hr ceiling).
* Oldest-first: operates on the lowest-numbered open PR.
* After a merge, immediately update-branches the new head so it re-validates
  against the new main and is ready for the next tick.
* Never acts while a dispatch is running, a dispatch recently completed, or a
  Slack conversation is live.  ``is_system_idle`` encodes this contract in one
  reusable place so future automation (deploy, restart) can inherit it.

Merge gate — ALL must hold before merging:
  1. PR has a non-author approving review OR the ``auto-merge`` label — UNLESS
     the PR carries an ``epic:*`` label, in which case it is excluded from both
     paths until it also carries ``epic-auto-merge`` (issue #753).
  2. All five required CI checks pass AND ``mergeable_state == "clean"``.
  3. System is idle (see above).

Branch-behind handling: if the head is behind main, ``update-branch`` it and
defer the merge to the next tick.  One branch-update per tick; never update the
whole queue at once.

The merge identity is ``aidt-merge`` via a dedicated PAT at ``MERGE_PAT_PATH``
(configurable via ``MERGE_QUEUE_PAT_PATH`` env var).
Token missing / 401 → fail loud (log + Slack), skip the tick entirely.

Continuous merge daemon (issue #832) — behind the default-off
``CONTINUOUS_MERGE`` setting, ``tick`` runs :func:`_continuous_tick` instead
of the legacy single-PR loop above. Every open PR is evaluated independently
each tick and dropped into one of three buckets — there is no ordered queue,
so a PR that cannot merge has zero effect on any other PR:

  * **A — auto-merge:** ``aidt-tl-sam`` approval on the *current* HEAD SHA +
    all required checks green + ``mergeable_state == "clean"`` + no
    ``SECURITY-manual`` label. Every eligible PR merges the same tick (no
    file-count cap, no one-per-tick cap).
  * **B — auto-rebase:** reviewed at HEAD but ``mergeable_state == "behind"``.
    Updated lazily — only PRs that are otherwise A-eligible — and capped at
    :data:`MAX_BRANCH_UPDATES_PER_TICK` per tick to avoid a rebase storm when
    a merge moves ``main``. Re-enters bucket A once green: the approval
    carries forward across the ``update-branch`` merge (see below), so a
    fresh CI pass on the rebased HEAD is the only thing standing between B
    and A.
  * **C — digest:** ``SECURITY-manual`` label (hard deny), a real conflict
    (``mergeable_state == "dirty"``), or not yet reviewed at HEAD. One
    consolidated Slack post per tick, never per-PR.

A PR whose CI is still running (checks pending, not yet clean/behind/dirty)
falls into none of the three buckets — it is silently re-evaluated next tick
rather than paging the digest for work that is already in flight.

Approval is read, never created: the loop only inspects the existing
``aidt-tl-sam`` review state tied to the current HEAD SHA (via the review's
``commit_id``) — if HEAD moved since the approval, the PR drops out of bucket
A into B or C, never merges on a stale review. One carve-out: if the *sole*
delta between the approved SHA and HEAD is a clean ``update-branch`` merge
from main (every intervening commit has 2 parents and introduces no author
change), the approval carries forward to the new HEAD — this is what lets a
rebased PR (bucket B) re-enter bucket A once green instead of dead-ending in
C. Any new non-merge author commit after the approved SHA still kills the
approval outright (reported to the digest as "needs re-review", distinct
from a PR that was never reviewed at all).

``CONTINUOUS_MERGE_DRY_RUN`` (defaults on) makes every bucket action
log-only — nothing is merged, rebased, or posted to Slack — so the first
tick after flipping ``CONTINUOUS_MERGE`` is a shadow run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from router import github_api, runtime, session_manager, settings, slack_post
from router.config import resolve_default_agent, resolve_session_timeout
from router.dispatch import state as dstate
from router.github_api import MERGE_PAT_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALLABLE_REF = "router.merge_queue:tick"

# Default-off hot flag gating the ChatAdapter status-post path (#838).
ENV_FLAG = "MERGE_QUEUE_STATUS_VIA_CHAT_ADAPTER"

# Transports with a live ChatAdapter resolver. Slack is deliberately absent —
# the Slack path always goes through the legacy slack_post call below (#838).
_ADAPTER_TRANSPORTS = frozenset({"discord"})
TASK_NAME = "idle-automerge"

MERGE_IDENTITY = "aidt-merge"

# Period for the scheduled system task (15 min).
DEFAULT_PERIOD_SECONDS = 900

# Idle window: no dispatch activity or Slack conversation in this window.
# Same numeric value as the SESSION_TIMEOUT registry default (router/settings.py)
# but intentionally independent — this gates merge-queue idleness, not session
# expiry, and has no registry entry of its own. Not a mirror; do not link it.
IDLE_WINDOW_SECONDS = 600  # 10 minutes

# Mergeability polling — GitHub computes mergeable_state lazily after a push to main,
# returning "unknown" until the background computation finishes.  Re-fetch a bounded
# number of times so the queue doesn't waste a full tick on a transient unknown.
MERGEABILITY_POLL_ATTEMPTS = 3
MERGEABILITY_POLL_INTERVAL_S = 15

# Required CI check names that must all pass before a merge is allowed.
REQUIRED_CHECKS: frozenset[str] = frozenset({"lint", "test-unit", "test-integration", "docker-build", "compose-check"})

# Continuous merge daemon (#832) — see module docstring.
SAM_LOGIN = "aidt-tl-sam"
SECURITY_LABEL = "SECURITY-manual"

# Cap on branch updates (bucket B) per continuous tick — merging one PR moves
# main and can put every other clean PR "behind" at once; only ever update a
# bounded number in a single tick so a big merge doesn't fire a rebase storm.
MAX_BRANCH_UPDATES_PER_TICK = 3


# Shared with auto_dispatch via router.github_api; the old private names are
# kept as aliases so call sites and test patch targets stay stable.
TokenError = github_api.TokenError
_read_pat = github_api.read_pat
_auth_headers = github_api.auth_headers
_gh_get = github_api.gh_get
_gh_get_all = github_api.gh_get_all
_gh_put = github_api.gh_put


async def _get_open_prs(repo: str, pat: str) -> list[dict]:
    """Return open PRs sorted oldest-first (ascending PR number)."""
    prs = await _gh_get_all(
        f"/repos/{repo}/pulls",
        pat,
        state="open",
        sort="created",
        direction="asc",
    )
    return sorted(prs, key=lambda p: p["number"])


async def _get_pr_details(repo: str, pr_num: int, pat: str) -> dict:
    """Fetch full PR details including ``mergeable_state``."""
    resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}", pat)
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 fetching PR #{pr_num} — check the merge PAT")
    resp.raise_for_status()
    return resp.json()


async def _fetch_all_reviews(repo: str, pr_num: int, pat: str) -> list[dict]:
    """Paginate and return the full review history for *pr_num*.

    GitHub's default page size (30) is far smaller than callers assume
    (unbounded) — this is the raw approval signal behind the merge-safety
    gate (#787), shared by every reviewed-at-HEAD check.
    """
    all_reviews: list[dict] = []
    page = 1
    while True:
        resp = await _gh_get(
            f"/repos/{repo}/pulls/{pr_num}/reviews",
            pat,
            per_page=100,
            page=page,
        )
        if resp.status_code == 401:
            raise TokenError(f"GitHub returned 401 fetching reviews for PR #{pr_num}")
        resp.raise_for_status()
        batch: list[dict] = resp.json()
        all_reviews.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return all_reviews


async def _has_approving_review(repo: str, pr_num: int, pr: dict, pat: str) -> bool:
    """Return True if the PR has a non-author approving review.

    Reduces the full review history to each non-author reviewer's latest effective
    state, ignoring COMMENTED and DISMISSED entries.  A later CHANGES_REQUESTED from
    the same reviewer blocks approval even when an earlier APPROVED exists.

    No label carve-outs here — this is the raw review signal, shared by
    ``_is_pr_approved`` (bug-loop / non-epic PRs, which layers the ``epic:*`` /
    ``auto-merge`` label carve-outs on top) and the epic orchestrator's Stage-3
    ``epic-auto-merge`` gate (#757), which needs the same "reviewed" signal
    for a PR that ``_is_pr_approved`` would otherwise short-circuit to False.
    """
    author_login = (pr.get("user") or {}).get("login", "")
    all_reviews = await _fetch_all_reviews(repo, pr_num, pat)

    # The API returns reviews in chronological order; iterating forward means the
    # last non-COMMENTED, non-DISMISSED state per login is the effective state.
    latest: dict[str, str] = {}
    for review in all_reviews:
        state = review.get("state", "")
        if state in ("COMMENTED", "DISMISSED"):
            continue
        login = (review.get("user") or {}).get("login", "")
        if not login or login == author_login:
            continue
        latest[login] = state

    if any(state == "CHANGES_REQUESTED" for state in latest.values()):
        return False
    return any(state == "APPROVED" for state in latest.values())


async def _is_pr_approved(repo: str, pr_num: int, pr: dict, pat: str) -> bool:
    """Return True if the PR has a non-author approving review OR the ``auto-merge`` label.

    Epic carve-out (#753): a PR carrying any ``epic:*`` label is excluded from
    auto-merge — including the ``auto-merge`` fast-path — until it also carries
    ``epic-auto-merge`` (added later by the Stage-3 gate, #757). This keeps
    feature PRs inside an epic from merging out of dependency order the instant
    Sam approves them. Non-``epic:*`` PRs are unaffected.
    """
    label_names = {lbl["name"] for lbl in pr.get("labels", [])}
    if any(name.startswith("epic:") for name in label_names) and "epic-auto-merge" not in label_names:
        return False
    if "auto-merge" in label_names:
        return True
    return await _has_approving_review(repo, pr_num, pr, pat)


async def _required_checks_passed(repo: str, head_sha: str, pat: str) -> bool:
    """Return True only when all five required CI checks have ``conclusion == 'success'``.

    Reduces to the latest run per check name (highest id) before evaluating
    conclusions, so a stale success from a superseded run cannot mask a
    current failure.  Paginates to collect all runs when more than 100 exist.
    """
    all_runs: list[dict] = []
    page = 1
    while True:
        resp = await _gh_get(
            f"/repos/{repo}/commits/{head_sha}/check-runs",
            pat,
            per_page=100,
            page=page,
        )
        if resp.status_code == 401:
            raise TokenError(f"GitHub returned 401 fetching check-runs for {head_sha}")
        resp.raise_for_status()
        batch: list[dict] = resp.json().get("check_runs", [])
        all_runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    # Keep only the latest run per check name (highest id = most recent).
    latest: dict[str, dict] = {}
    for run in all_runs:
        name = run["name"]
        if name not in latest or run.get("id", 0) > latest[name].get("id", 0):
            latest[name] = run

    passed = {name for name, run in latest.items() if run.get("conclusion") == "success"}
    return REQUIRED_CHECKS.issubset(passed)


async def _update_branch(repo: str, pr_num: int, pat: str) -> bool:
    """Trigger a branch update for *pr_num*.

    Returns True on success (202), False on conflict (422).
    Raises on other unexpected errors.
    """
    resp = await _gh_put(f"/repos/{repo}/pulls/{pr_num}/update-branch", pat)
    if resp.status_code == 202:
        return True
    if resp.status_code == 422:
        return False
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 on update-branch for PR #{pr_num}")
    resp.raise_for_status()
    return False


async def _squash_merge(repo: str, pr_num: int, pr_title: str, head_sha: str, pat: str) -> bool | None:
    """Squash-merge *pr_num* under the ``aidt-merge`` identity.

    Passes *head_sha* as the ``sha`` parameter on the merge PUT so GitHub
    rejects (409) if the branch head moved between validation and the merge
    call (optimistic-concurrency guard for issue #513).

    Returns True on success (200), None when the head moved (409 — drop back
    to re-validation on the next tick), False when GitHub otherwise refuses
    the merge.  Raises :class:`TokenError` on 401.
    """
    body = {
        "merge_method": "squash",
        "commit_title": f"{pr_title} (#{pr_num})",
        "sha": head_sha,
    }
    resp = await _gh_put(f"/repos/{repo}/pulls/{pr_num}/merge", pat, body)
    if resp.status_code == 200:
        return True
    if resp.status_code == 409:
        logger.warning(
            "merge_queue: PR #%s head moved during merge (409) — re-queuing for next tick",
            pr_num,
        )
        return None
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 on merge for PR #{pr_num}")
    logger.warning(
        "merge_queue: squash merge returned %s for PR #%s — %s",
        resp.status_code,
        pr_num,
        resp.text[:200],
    )
    return False


async def _verify_merged(repo: str, pr_num: int, pat: str) -> bool:
    """Verify the merge succeeded by re-reading PR state from the API.

    Returns True when ``state == "closed"``, ``merged == True``, and
    ``merged_by.login == MERGE_IDENTITY``.  Narration is not sufficient —
    GitHub is the ground truth.
    """
    pr = await _get_pr_details(repo, pr_num, pat)
    if pr.get("state") != "closed":
        return False
    if not pr.get("merged"):
        return False
    merged_by = (pr.get("merged_by") or {}).get("login", "")
    if merged_by != MERGE_IDENTITY:
        logger.warning(
            "merge_queue: PR #%s merged but merged_by=%r (expected %r)",
            pr_num,
            merged_by,
            MERGE_IDENTITY,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Continuous merge daemon (#832)
# ---------------------------------------------------------------------------


async def _is_clean_main_merge_only(repo: str, pat: str, approved_sha: str, head_sha: str) -> bool:
    """Return True iff every commit between *approved_sha* and *head_sha* is a
    merge commit (2 parents) — i.e. HEAD only moved because ``update-branch``
    pulled main forward, with no new author commit landed since the review
    (#832 amend: carry-forward carve-out).

    Uses the GitHub compare API (``approved_sha...head_sha``) rather than
    inspecting diff content: a merge commit's second parent is the incoming
    ref, so any commit in the range with a single parent is necessarily a new
    author commit, not part of the main-merge. No commits in range (e.g. a
    force-push back to an old SHA) is conservatively treated as not carrying.
    """
    resp = await _gh_get(f"/repos/{repo}/compare/{approved_sha}...{head_sha}", pat)
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 comparing {approved_sha}...{head_sha}")
    resp.raise_for_status()
    commits: list[dict] = resp.json().get("commits", [])
    if not commits:
        return False
    return all(len(commit.get("parents", [])) >= 2 for commit in commits)


async def _review_status_at_head(repo: str, pr_num: int, pat: str, head_sha: str) -> tuple[bool, str]:
    """Return ``(approved, reason)`` for ``aidt-tl-sam``'s review at *head_sha*.

    Reduces to the last non-COMMENTED, non-DISMISSED review by that login
    (same reduction as :func:`_has_approving_review`). A later
    CHANGES_REQUESTED overrides an earlier APPROVED. When the APPROVED
    review's own ``commit_id`` doesn't match *head_sha* (branch moved after
    the review), the approval still carries forward if the only delta since
    is a clean main-merge (:func:`_is_clean_main_merge_only`) — otherwise a
    new author commit landed after the approval and it is dead.

    ``reason`` is only meaningful when *approved* is False:

    * ``"unreviewed"`` — never approved by aidt-tl-sam (or the latest state
      is CHANGES_REQUESTED).
    * ``"needs_re_review"`` — approved at an earlier SHA, but a new author
      commit landed since, so the approval could not carry forward.
    """
    all_reviews = await _fetch_all_reviews(repo, pr_num, pat)

    state = ""
    commit_id = ""
    for review in all_reviews:
        if (review.get("user") or {}).get("login") != SAM_LOGIN:
            continue
        review_state = review.get("state", "")
        if review_state in ("COMMENTED", "DISMISSED"):
            continue
        state = review_state
        commit_id = review.get("commit_id", "")

    if state != "APPROVED":
        return False, "unreviewed"
    if commit_id == head_sha:
        return True, "approved"
    if await _is_clean_main_merge_only(repo, pat, commit_id, head_sha):
        return True, "approved"
    return False, "needs_re_review"


async def _sam_approved_at_head(repo: str, pr_num: int, pat: str, head_sha: str) -> bool:
    """Return True iff ``aidt-tl-sam``'s approval is valid at the *current*
    HEAD SHA — either the review's own ``commit_id`` matches, or it carries
    forward across a clean main-merge (see :func:`_review_status_at_head`).
    """
    approved, _ = await _review_status_at_head(repo, pr_num, pat, head_sha)
    return approved


async def _classify_pr(repo: str, pr: dict, pat: str) -> tuple[str, str]:
    """Partition one PR into a continuous-merge bucket.

    Returns ``(bucket, reason)`` where ``bucket`` is one of:

    * ``"A"`` — auto-merge eligible.
    * ``"B"`` — auto-rebase (reviewed at HEAD, behind main).
    * ``"C"`` — digest (SECURITY-manual, conflict, or not reviewed at HEAD).
    * ``"pending"`` — none of the above; still resolving (e.g. checks
      running). Re-evaluated next tick, never pinged.

    Evaluation order matters: SECURITY-manual and real conflicts short-circuit
    to C before spending an API call on the review check, and the review
    check runs before the checks/mergeable-state checks so a PR that simply
    hasn't been reviewed yet is reported as "unreviewed" rather than
    "pending".
    """
    pr_num = pr["number"]
    label_names = {lbl["name"] for lbl in pr.get("labels", [])}

    if SECURITY_LABEL in label_names:
        return "C", "security_manual"

    mergeable_state = pr.get("mergeable_state") or "unknown"
    if mergeable_state == "dirty":
        return "C", "conflict"

    head_sha = (pr.get("head") or {}).get("sha", "")
    approved, review_reason = await _review_status_at_head(repo, pr_num, pat, head_sha)
    if not approved:
        return "C", review_reason

    if mergeable_state == "behind":
        return "B", "behind"

    if mergeable_state != "clean":
        return "pending", mergeable_state

    if not await _required_checks_passed(repo, head_sha, pat):
        return "pending", "checks_pending"

    return "A", "eligible"


async def _continuous_tick(
    *,
    repo: str,
    pat: str,
    slack_client: Any,
    destination: str | None,
    dry_run: bool,
) -> dict:
    """Continuous-merge mode (issue #832). See module docstring for the
    bucket contract. Independent per-PR partition: a PR that errors or lands
    in bucket C never prevents any other PR from merging or rebasing this
    tick.
    """
    try:
        prs = await _get_open_prs(repo, pat)
    except TokenError as exc:
        msg = f":x: merge-queue: {exc}"
        logger.error("merge_queue: %s", exc)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "skipped": "token_error"}
    except httpx.HTTPError as exc:
        logger.error("merge_queue: continuous: HTTP error listing PRs: %s", exc)
        return {"status": "ok", "skipped": "http_error"}

    if not prs:
        logger.info("merge_queue: continuous: no open PRs")
        return {"status": "ok", "skipped": "no_open_prs"}

    merged: list[int] = []
    rebased: list[int] = []
    digest: list[tuple[int, str, str]] = []

    for pr_summary in prs:
        pr_num: int = pr_summary["number"]

        try:
            pr = await _get_pr_details(repo, pr_num, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.warning("merge_queue: continuous: HTTP error fetching PR #%s, skipping: %s", pr_num, exc)
            continue

        pr_title: str = pr.get("title") or f"PR #{pr_num}"

        try:
            bucket, reason = await _classify_pr(repo, pr, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.warning("merge_queue: continuous: HTTP error classifying PR #%s, skipping: %s", pr_num, exc)
            continue

        logger.info("merge_queue: continuous: PR #%s -> bucket=%s reason=%s", pr_num, bucket, reason)

        if bucket == "pending":
            continue

        if bucket == "C":
            digest.append((pr_num, reason, pr_title))
            continue

        if bucket == "B":
            if len(rebased) >= MAX_BRANCH_UPDATES_PER_TICK:
                logger.info("merge_queue: continuous: branch-update cap reached, deferring PR #%s", pr_num)
                continue
            if dry_run:
                logger.info("merge_queue: continuous: [dry-run] would update-branch PR #%s", pr_num)
                rebased.append(pr_num)
                continue
            try:
                updated = await _update_branch(repo, pr_num, pat)
            except TokenError as exc:
                msg = f":x: merge-queue: {exc}"
                logger.error("merge_queue: %s", exc)
                await _slack_post(slack_client, destination, msg)
                return {"status": "ok", "skipped": "token_error"}
            except httpx.HTTPError as exc:
                logger.warning("merge_queue: continuous: HTTP error updating branch PR #%s: %s", pr_num, exc)
                continue
            if updated:
                rebased.append(pr_num)
            continue

        # bucket == "A"
        head_sha: str = (pr.get("head") or {}).get("sha", "")
        if dry_run:
            logger.info("merge_queue: continuous: [dry-run] would squash-merge PR #%s (%s)", pr_num, pr_title)
            merged.append(pr_num)
            continue

        try:
            result = await _squash_merge(repo, pr_num, pr_title, head_sha, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.warning("merge_queue: continuous: HTTP error merging PR #%s: %s", pr_num, exc)
            continue

        if result is None:
            logger.info("merge_queue: continuous: PR #%s head moved during merge (409) — re-queuing", pr_num)
            continue
        if not result:
            logger.error("merge_queue: continuous: squash merge refused by GitHub for PR #%s", pr_num)
            continue

        try:
            verified = await _verify_merged(repo, pr_num, pat)
        except (TokenError, httpx.HTTPError) as exc:
            logger.error("merge_queue: continuous: could not verify merge for PR #%s: %s", pr_num, exc)
            continue

        if not verified:
            logger.error("merge_queue: continuous: could not verify merge for PR #%s", pr_num)
            continue

        logger.info("merge_queue: continuous: merged PR #%s as %s", pr_num, MERGE_IDENTITY)
        merged.append(pr_num)

    if digest:
        if dry_run:
            logger.info(
                "merge_queue: continuous: [dry-run] would post digest for %s PR(s): %s",
                len(digest),
                ", ".join(f"#{n} ({reason})" for n, reason, _ in digest),
            )
        else:
            lines = [f":mega: merge-queue digest — {len(digest)} PR(s) need attention:"]
            lines.extend(f"• #{pr_num} ({reason}) — {pr_title}" for pr_num, reason, pr_title in digest)
            await _slack_post(slack_client, destination, "\n".join(lines))

    return {
        "status": "ok",
        "action": "continuous",
        "merged": merged,
        "rebased": rebased,
        "digest": [pr_num for pr_num, _, _ in digest],
    }


# ---------------------------------------------------------------------------
# Idle detection — reusable property
# ---------------------------------------------------------------------------


def is_system_idle(
    *,
    now: datetime | None = None,
    dispatch_root_override: str | None = None,
    window_seconds: int = IDLE_WINDOW_SECONDS,
    session_timeout: int | None = None,
) -> tuple[bool, str | None]:
    """Return ``(True, None)`` when the system is idle, ``(False, reason)`` otherwise.

    Idle requires ALL of:
    1. No active dispatches — no dispatch workspace missing an ``exitcode`` file.
    2. No dispatch completion in the last ``window_seconds`` — the ``exitcode``
       file mtime is outside the window.
    3. No Slack conversation activity in the last ``window_seconds`` — no session
       whose ``last_activity`` falls within the window.

    This function is the single source of truth for the "never act while a
    conversation is active" invariant.  Future automation (deploy, restart) should
    import and call it rather than re-implementing the check.
    """
    now_ts = now.timestamp() if now is not None else time.time()

    for dispatch_id in dstate.list_dispatch_ids(root=dispatch_root_override):
        exitcode = dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root_override)
        if exitcode is None:
            # No exitcode yet — check whether the slot is actually alive.
            if dstate.is_dispatch_stale(dispatch_id, root=dispatch_root_override, now=now_ts):
                logger.info("merge_queue: reaped stale slot %s", dispatch_id)
                dstate.reap_stale_dispatch(dispatch_id, root=dispatch_root_override, now=now_ts)
                continue
            return False, f"active_dispatch:{dispatch_id}"

        # Dispatch is terminal; check how recently it completed.
        exitcode_path = dstate.dispatch_dir(dispatch_id, root=dispatch_root_override) / dstate.FIELD_EXITCODE
        try:
            mtime = exitcode_path.stat().st_mtime
        except OSError:
            continue
        if (now_ts - mtime) < window_seconds:
            return False, f"recent_completion:{dispatch_id}"

    active_sessions = session_manager.get_active_sessions(timeout_seconds=session_timeout)
    if active_sessions:
        return False, "active_conversation"

    return True, None


# ---------------------------------------------------------------------------
# Post helper
# ---------------------------------------------------------------------------


def _chat_adapter_status_enabled() -> bool:
    """Return True when MERGE_QUEUE_STATUS_VIA_CHAT_ADAPTER is truthy (hot-reloadable)."""
    return bool(settings.get(ENV_FLAG))


async def _post_via_chat_adapter(transport: str, conversation_ref: str, text: str) -> bool:
    from router.chat.types import ConversationRef, OutboundMessage

    try:
        agent = resolve_default_agent()
    except ValueError:
        logger.warning("merge_queue: no agent configured; skipping ChatAdapter post")
        return False

    adapter = runtime.discord_adapter_for_agent(agent)
    if adapter is None:
        logger.warning("merge_queue: no %s adapter for agent=%s; skipping post", transport, agent)
        return False
    try:
        await adapter.send_message(OutboundMessage(text=text, conversation_ref=ConversationRef(conversation_ref)))
    except Exception:
        logger.exception("merge_queue: ChatAdapter post failed agent=%s transport=%s", agent, transport)
        return False
    return True


async def _slack_post(slack_client: Any, channel: str | None, text: str) -> None:
    """Post *text* via the best-available transport for merge-queue status. Never raises.

    Behind the default-off ``MERGE_QUEUE_STATUS_VIA_CHAT_ADAPTER`` flag (#838, mirrors
    ``router.dispatch.feed_transport``'s #713 pattern), a stored non-Slack
    ``MERGE_QUEUE_TRANSPORT``/``MERGE_QUEUE_CONVERSATION_REF`` pair routes the post through
    that ChatAdapter instead. Flag off, an unset/Slack transport, or a missing conversation_ref
    all degrade to the historical ``slack_post.best_effort_post`` call, byte-for-byte — this is
    the single choke point every merge-queue call site posts through. An unresolvable or
    unsupported transport skips the post with a clear log line; it never silently falls back to
    Slack (that would post into the wrong conversation).
    """
    if _chat_adapter_status_enabled():
        transport = (settings.get("MERGE_QUEUE_TRANSPORT") or "").strip()
        if transport and transport != "slack":
            conversation_ref = (settings.get("MERGE_QUEUE_CONVERSATION_REF") or "").strip()
            if not conversation_ref:
                logger.info("merge_queue: missing conversation_ref for transport=%s; skipping post", transport)
                return
            if transport not in _ADAPTER_TRANSPORTS:
                logger.warning("merge_queue: unsupported transport=%r; skipping post", transport)
                return
            await _post_via_chat_adapter(transport, conversation_ref, text)
            return

    await slack_post.best_effort_post(slack_client, channel, text, log=logger, prefix="merge_queue")


# ---------------------------------------------------------------------------
# Main tick callable
# ---------------------------------------------------------------------------


async def tick(*, payload: dict, slack_client: Any, now: datetime) -> dict:
    """System-task callable invoked by the scheduler every 15 minutes.

    Iterates open PRs oldest→newest and merges the first fully-eligible one,
    so a blocked head-of-line PR does not park the entire queue (issue #540).
    Always returns ``{"status": "ok"}`` — the task is permanent and must
    never be deregistered by returning ``{"status": "done"}``.
    """
    repo: str = payload.get("repo", "")
    pat_path: str = payload.get("pat_path", MERGE_PAT_PATH)
    # Resolved per tick so a MERGE_QUEUE_CHANNEL change in the runtime config
    # applies on the next tick without re-registering the task (#576). The
    # payload value (baked in at registration) is only a fallback.
    destination: str | None = (
        settings.get("MERGE_QUEUE_CHANNEL") or payload.get("destination") or settings.get("OPERATOR_DM_CHANNEL") or None
    )

    if not repo:
        logger.error("merge_queue: payload missing 'repo'; skipping tick")
        return {"status": "ok", "skipped": "no_repo"}

    # --- 1. Read PAT — fail loud on any problem, never fall through silently. ---
    try:
        pat = _read_pat(pat_path)
    except TokenError as exc:
        msg = f":x: merge-queue: {exc}"
        logger.error("merge_queue: %s", exc)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "skipped": "token_error"}

    # --- 2. Idle guard. ---
    # Thread the configured session timeout through so the idle view uses the
    # same expiry boundary as routing/cleanup (issue #462). Resolved at tick
    # time from the live SESSION_TIMEOUT env rather than baked into the
    # persisted task payload, so config changes take effect without re-register.
    idle, idle_reason = is_system_idle(now=now, session_timeout=resolve_session_timeout())
    if not idle:
        logger.info("merge_queue: system not idle (%s), skipping tick", idle_reason)
        return {"status": "ok", "skipped": idle_reason}

    # --- 2b. Continuous merge daemon (#832), behind a default-off flag. ---
    # Independent per-PR bucket partition instead of the legacy single-PR
    # loop below — see module docstring and _continuous_tick.
    if settings.get("CONTINUOUS_MERGE"):
        return await _continuous_tick(
            repo=repo,
            pat=pat,
            slack_client=slack_client,
            destination=destination,
            dry_run=bool(settings.get("CONTINUOUS_MERGE_DRY_RUN")),
        )

    # --- 3. Get open PRs sorted oldest-first. ---
    try:
        prs = await _get_open_prs(repo, pat)
    except TokenError as exc:
        msg = f":x: merge-queue: {exc}"
        logger.error("merge_queue: %s", exc)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "skipped": "token_error"}
    except httpx.HTTPError as exc:
        logger.error("merge_queue: HTTP error listing PRs: %s", exc)
        return {"status": "ok", "skipped": "http_error"}

    if not prs:
        logger.info("merge_queue: no open PRs")
        return {"status": "ok", "skipped": "no_open_prs"}

    # --- 4. Iterate oldest→newest; act on the first fully-eligible PR. ---
    #
    # Eligibility gates (unchanged): mergeable_state=="clean", required CI
    # checks all passing, approved, and system idle (checked above).
    #
    # Special cases that end the tick immediately:
    #   • "behind": update-branch and defer — one branch-update per tick.
    #   • TokenError / HTTP error: fail loud and abort.
    #
    # All other ineligible states (not-clean, ci-not-green, not-approved,
    # still-unknown after polling) skip to the next PR instead of parking
    # the whole queue.
    for idx, pr_summary in enumerate(prs):
        pr_num: int = pr_summary["number"]

        # Fetch full PR details (includes mergeable_state).
        try:
            pr = await _get_pr_details(repo, pr_num, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.error("merge_queue: HTTP error fetching PR #%s: %s", pr_num, exc)
            return {"status": "ok", "skipped": "http_error"}

        mergeable_state: str = pr.get("mergeable_state") or "unknown"

        # Poll if mergeability is transient unknown.
        if mergeable_state == "unknown":
            logger.info(
                "merge_queue: PR #%s mergeable_state=unknown; polling up to %s times",
                pr_num,
                MERGEABILITY_POLL_ATTEMPTS,
            )
            for _ in range(MERGEABILITY_POLL_ATTEMPTS):
                await asyncio.sleep(MERGEABILITY_POLL_INTERVAL_S)
                try:
                    pr = await _get_pr_details(repo, pr_num, pat)
                except TokenError as exc:
                    msg = f":x: merge-queue: {exc}"
                    logger.error("merge_queue: %s", exc)
                    await _slack_post(slack_client, destination, msg)
                    return {"status": "ok", "skipped": "token_error"}
                except httpx.HTTPError as exc:
                    logger.error(
                        "merge_queue: HTTP error re-fetching PR #%s during unknown-state poll: %s",
                        pr_num,
                        exc,
                    )
                    return {"status": "ok", "skipped": "http_error"}
                mergeable_state = pr.get("mergeable_state") or "unknown"
                if mergeable_state != "unknown":
                    break

        pr_title: str = pr.get("title") or f"PR #{pr_num}"
        head_sha: str = (pr.get("head") or {}).get("sha", "")

        logger.info("merge_queue: PR #%s mergeable_state=%s", pr_num, mergeable_state)

        # Behind: update branch and end this tick (one branch-update per tick).
        if mergeable_state == "behind":
            logger.info("merge_queue: PR #%s is behind base; updating branch", pr_num)
            try:
                updated = await _update_branch(repo, pr_num, pat)
            except TokenError as exc:
                msg = f":x: merge-queue: {exc}"
                logger.error("merge_queue: %s", exc)
                await _slack_post(slack_client, destination, msg)
                return {"status": "ok", "skipped": "token_error"}
            except httpx.HTTPError as exc:
                logger.error("merge_queue: HTTP error on update-branch for PR #%s: %s", pr_num, exc)
                return {"status": "ok", "skipped": "http_error"}

            if not updated:
                msg = f":warning: merge-queue: PR #{pr_num} needs manual rebase (conflict updating branch)"
                logger.warning("merge_queue: update-branch conflict for PR #%s", pr_num)
                await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "action": "branch_updated", "pr": pr_num}

        # Not clean (blocked, dirty, unknown-after-poll, etc.) → skip to next PR.
        if mergeable_state != "clean":
            logger.info(
                "merge_queue: PR #%s not mergeable (mergeable_state=%s), skipping to next",
                pr_num,
                mergeable_state,
            )
            continue

        # CI check (belt-and-suspenders with mergeable_state==clean).
        try:
            ci_passed = await _required_checks_passed(repo, head_sha, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.error("merge_queue: HTTP error checking CI for PR #%s: %s", pr_num, exc)
            return {"status": "ok", "skipped": "http_error"}

        if not ci_passed:
            logger.info("merge_queue: PR #%s required CI checks not all passing, skipping to next", pr_num)
            continue

        # Approval check.
        try:
            approved = await _is_pr_approved(repo, pr_num, pr, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.error("merge_queue: HTTP error checking approval for PR #%s: %s", pr_num, exc)
            return {"status": "ok", "skipped": "http_error"}

        if not approved:
            logger.info("merge_queue: PR #%s not approved, skipping to next", pr_num)
            continue

        # --- 5. Squash-merge the first eligible PR. ---
        logger.info("merge_queue: squash-merging PR #%s (%s)", pr_num, pr_title)
        try:
            merged = await _squash_merge(repo, pr_num, pr_title, head_sha, pat)
        except TokenError as exc:
            msg = f":x: merge-queue: {exc}"
            logger.error("merge_queue: %s", exc)
            await _slack_post(slack_client, destination, msg)
            return {"status": "ok", "skipped": "token_error"}
        except httpx.HTTPError as exc:
            logger.error("merge_queue: HTTP error merging PR #%s: %s", pr_num, exc)
            return {"status": "ok", "skipped": "http_error"}

        if merged is None:
            logger.info("merge_queue: PR #%s dropped back to re-validation (head moved, 409)", pr_num)
            return {"status": "ok", "action": "head_moved", "pr": pr_num}

        if not merged:
            logger.error("merge_queue: squash merge refused by GitHub for PR #%s", pr_num)
            return {"status": "ok", "action": "merge_refused", "pr": pr_num}

        # --- 6. Verify merge — re-read PR state from API. ---
        try:
            verified = await _verify_merged(repo, pr_num, pat)
        except TokenError as exc:
            logger.error("merge_queue: token error verifying PR #%s: %s", pr_num, exc)
            return {"status": "ok", "action": "merge_unverified", "pr": pr_num}
        except httpx.HTTPError as exc:
            logger.error("merge_queue: HTTP error verifying PR #%s: %s", pr_num, exc)
            return {"status": "ok", "action": "merge_unverified", "pr": pr_num}

        if not verified:
            logger.error("merge_queue: could not verify merge for PR #%s (state or merged_by mismatch)", pr_num)
            return {"status": "ok", "action": "merge_unverified", "pr": pr_num}

        logger.info("merge_queue: merged PR #%s as %s", pr_num, MERGE_IDENTITY)

        # --- 7. Update-branch the new head (oldest remaining open PR). ---
        # prs[:idx] contains any PRs that were skipped before the merged one;
        # prs[idx+1:] contains PRs after it.  The new head is the lowest-numbered
        # remaining open PR, i.e. the first element of the combined remainder.
        remaining = prs[:idx] + prs[idx + 1 :]
        if remaining:
            next_pr_num = remaining[0]["number"]
            logger.info("merge_queue: updating branch for next head PR #%s", next_pr_num)
            try:
                await _update_branch(repo, next_pr_num, pat)
            except (TokenError, httpx.HTTPError) as exc:
                logger.warning("merge_queue: failed to update-branch next head PR #%s: %s", next_pr_num, exc)

        return {"status": "ok", "action": "merged", "pr": pr_num}

    # No PR in the queue was eligible this tick.
    logger.info("merge_queue: no eligible PR found in queue")
    return {"status": "ok", "skipped": "no_eligible_pr"}


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def register_merge_queue(
    store: Any,
    *,
    agent_name: str,
    repo: str,
    pat_path: str = MERGE_PAT_PATH,
    destination: str | None = None,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
) -> Any:
    """Idempotently register the merge-queue system task in *store*.

    Safe to call on every router boot — if the task already exists under
    :data:`CALLABLE_REF` it is returned untouched rather than duplicated.

    ``agent_name`` determines which Slack client the scheduler resolves for
    posting; ``destination`` is the channel for error/conflict notifications
    (falls back to ``OPERATOR_DM_CHANNEL`` at tick time if omitted here).
    """
    existing = store.list_by_callable_ref(CALLABLE_REF)
    if existing:
        logger.debug("merge_queue: system task already registered (%s)", existing[0].task_id)
        return existing[0]

    payload: dict[str, Any] = {
        "repo": repo,
        "pat_path": pat_path,
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
        "merge_queue: registered system task task_id=%s repo=%s period=%ss agent=%s",
        task.task_id,
        repo,
        period_seconds,
        agent_name,
    )
    return task
