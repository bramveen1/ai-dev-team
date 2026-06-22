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
  1. PR has a non-author approving review OR the ``auto-merge`` label.
  2. All five required CI checks pass AND ``mergeable_state == "clean"``.
  3. System is idle (see above).

Branch-behind handling: if the head is behind main, ``update-branch`` it and
defer the merge to the next tick.  One branch-update per tick; never update the
whole queue at once.

The merge identity is ``aidt-merge`` via a dedicated PAT at ``MERGE_PAT_PATH``
(configurable via ``MERGE_QUEUE_PAT_PATH`` env var).
Token missing / 401 → fail loud (log + Slack), skip the tick entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from router import session_manager
from router.config import resolve_session_timeout
from router.dispatch import state as dstate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALLABLE_REF = "router.merge_queue:tick"
TASK_NAME = "idle-automerge"

# Default PAT file location — one-line change to swap the merge identity.
MERGE_PAT_PATH = "/config/secrets/gh-aidt-merge.token"

MERGE_IDENTITY = "aidt-merge"
GITHUB_API_BASE = "https://api.github.com"

# Period for the scheduled system task (15 min).
DEFAULT_PERIOD_SECONDS = 900

# Idle window: no dispatch activity or Slack conversation in this window.
IDLE_WINDOW_SECONDS = 600  # 10 minutes

# Mergeability polling — GitHub computes mergeable_state lazily after a push to main,
# returning "unknown" until the background computation finishes.  Re-fetch a bounded
# number of times so the queue doesn't waste a full tick on a transient unknown.
MERGEABILITY_POLL_ATTEMPTS = 3
MERGEABILITY_POLL_INTERVAL_S = 15

# Required CI check names that must all pass before a merge is allowed.
REQUIRED_CHECKS: frozenset[str] = frozenset({"lint", "test-unit", "test-integration", "docker-build", "compose-check"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MergeQueueError(Exception):
    """Base error for merge-queue failures."""


class TokenError(MergeQueueError):
    """Raised when the merge PAT is missing, empty, or rejected (401)."""


# ---------------------------------------------------------------------------
# PAT helpers
# ---------------------------------------------------------------------------


def _read_pat(path: str = MERGE_PAT_PATH) -> str:
    """Read the merge PAT from *path*.

    Raises :class:`TokenError` if the file is missing, unreadable, or empty.
    Never silently falls through to cached credentials — that is the #298
    footgun this check was designed to prevent.
    """
    try:
        token = Path(path).read_text().strip()
    except FileNotFoundError:
        raise TokenError(f"PAT file not found: {path}") from None
    except OSError as exc:
        raise TokenError(f"Cannot read PAT file {path}: {exc}") from exc
    if not token:
        raise TokenError(f"PAT file is empty: {path}")
    return token


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _auth_headers(pat: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _gh_get(path: str, pat: str, **params: Any) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.get(url, headers=_auth_headers(pat), params=params)


async def _gh_put(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.put(url, headers=_auth_headers(pat), json=body or {})


async def _get_open_prs(repo: str, pat: str) -> list[dict]:
    """Return open PRs sorted oldest-first (ascending PR number)."""
    resp = await _gh_get(
        f"/repos/{repo}/pulls",
        pat,
        state="open",
        sort="created",
        direction="asc",
        per_page=100,
    )
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 for {repo} — check the merge PAT")
    resp.raise_for_status()
    prs: list[dict] = resp.json()
    return sorted(prs, key=lambda p: p["number"])


async def _get_pr_details(repo: str, pr_num: int, pat: str) -> dict:
    """Fetch full PR details including ``mergeable_state``."""
    resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}", pat)
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 fetching PR #{pr_num} — check the merge PAT")
    resp.raise_for_status()
    return resp.json()


async def _is_pr_approved(repo: str, pr_num: int, pr: dict, pat: str) -> bool:
    """Return True if the PR has a non-author approving review OR the ``auto-merge`` label."""
    label_names = {lbl["name"] for lbl in pr.get("labels", [])}
    if "auto-merge" in label_names:
        return True
    author_login = (pr.get("user") or {}).get("login", "")
    resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}/reviews", pat)
    if resp.status_code == 401:
        raise TokenError(f"GitHub returned 401 fetching reviews for PR #{pr_num}")
    resp.raise_for_status()
    for review in resp.json():
        if review.get("state") == "APPROVED" and review.get("user", {}).get("login") != author_login:
            return True
    return False


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


async def _squash_merge(repo: str, pr_num: int, pr_title: str, pat: str) -> bool:
    """Squash-merge *pr_num* under the ``aidt-merge`` identity.

    Returns True on success (200), False when GitHub refuses the merge.
    Raises :class:`TokenError` on 401.
    """
    body = {
        "merge_method": "squash",
        "commit_title": f"{pr_title} (#{pr_num})",
    }
    resp = await _gh_put(f"/repos/{repo}/pulls/{pr_num}/merge", pat, body)
    if resp.status_code == 200:
        return True
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
            # Dispatch is still running.
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


async def _slack_post(slack_client: Any, channel: str | None, text: str) -> None:
    """Best-effort Slack post.  Never raises — notifications must not wedge the tick."""
    if slack_client is None or not channel:
        logger.info("merge_queue: no Slack client/channel; skipping post: %s", text)
        return
    try:
        await slack_client.chat_postMessage(channel=channel, text=text)
    except Exception:
        logger.exception("merge_queue: failed to post notification")


# ---------------------------------------------------------------------------
# Main tick callable
# ---------------------------------------------------------------------------


async def tick(*, payload: dict, slack_client: Any, now: datetime) -> dict:
    """System-task callable invoked by the scheduler every 15 minutes.

    Implements the head-of-queue merge logic described in issue #437.
    Always returns ``{"status": "ok"}`` — the task is permanent and must
    never be deregistered by returning ``{"status": "done"}``.
    """
    repo: str = payload.get("repo", "")
    pat_path: str = payload.get("pat_path", MERGE_PAT_PATH)
    destination: str | None = payload.get("destination") or os.environ.get("BRAM_DM_CHANNEL")

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

    # --- 3. Get head-of-queue (oldest open PR). ---
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

    head_summary = prs[0]
    pr_num: int = head_summary["number"]

    # --- 4. Fetch full PR details (includes mergeable_state). ---
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

    # --- 4a. Poll if mergeability is transient unknown. ---
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

    logger.info("merge_queue: head PR #%s mergeable_state=%s", pr_num, mergeable_state)

    # --- 5. Branch-behind handling — update and defer merge to next tick. ---
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

    # --- 6. Gate check: approval + CI green. ---
    if mergeable_state != "clean":
        logger.info(
            "merge_queue: PR #%s not mergeable (mergeable_state=%s), skipping",
            pr_num,
            mergeable_state,
        )
        return {"status": "ok", "skipped": "not_mergeable", "pr": pr_num}

    # Check CI checks explicitly (belt-and-suspenders with mergeable_state==clean).
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
        logger.info("merge_queue: PR #%s required CI checks not all passing, skipping", pr_num)
        return {"status": "ok", "skipped": "ci_not_green", "pr": pr_num}

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
        logger.info("merge_queue: PR #%s not approved, skipping", pr_num)
        return {"status": "ok", "skipped": "not_approved", "pr": pr_num}

    # --- 7. Squash-merge. ---
    logger.info("merge_queue: squash-merging PR #%s (%s)", pr_num, pr_title)
    try:
        merged = await _squash_merge(repo, pr_num, pr_title, pat)
    except TokenError as exc:
        msg = f":x: merge-queue: {exc}"
        logger.error("merge_queue: %s", exc)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "skipped": "token_error"}
    except httpx.HTTPError as exc:
        logger.error("merge_queue: HTTP error merging PR #%s: %s", pr_num, exc)
        return {"status": "ok", "skipped": "http_error"}

    if not merged:
        logger.error("merge_queue: squash merge refused by GitHub for PR #%s", pr_num)
        return {"status": "ok", "action": "merge_refused", "pr": pr_num}

    # --- 8. Verify merge — re-read PR state from API. ---
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

    # --- 9. Update-branch the new head so it re-validates against new main. ---
    next_prs = prs[1:]
    if next_prs:
        next_pr_num = next_prs[0]["number"]
        logger.info("merge_queue: updating branch for next head PR #%s", next_pr_num)
        try:
            await _update_branch(repo, next_pr_num, pat)
        except (TokenError, httpx.HTTPError) as exc:
            logger.warning("merge_queue: failed to update-branch next head PR #%s: %s", next_pr_num, exc)

    return {"status": "ok", "action": "merged", "pr": pr_num}


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
    (falls back to ``BRAM_DM_CHANNEL`` at tick time if omitted here).
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
