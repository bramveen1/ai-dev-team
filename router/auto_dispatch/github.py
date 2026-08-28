"""GitHub reads/writes for the auto-dispatch loop.

Thin, single-purpose wrappers over ``router.github_api`` (shared with
merge_queue): listing bug issues and dev PRs, reading CI check runs and
verdict comments, and applying the ``auto-merge`` label. The old private
names (``_gh_get`` etc.) are kept as aliases so call sites and test patch
targets stay stable.
"""

from __future__ import annotations

import logging
import re

from router import github_api, settings
from router.auto_dispatch.config import (
    AC_SECTION_RE,
    AUTO_MERGE_LABEL,
    DEV_WORKER_BRANCH_PREFIX,
    REQUIRED_CHECKS,
)
from router.auto_dispatch.triage import _pre_dispatch_triage

logger = logging.getLogger(__name__)

_TokenError = github_api.TokenError
_read_pat = github_api.read_pat
_auth_headers = github_api.auth_headers
_gh_get = github_api.gh_get
_gh_get_all = github_api.gh_get_all
_gh_put = github_api.gh_put
_gh_post = github_api.gh_post


async def _get_open_bug_issues(repo: str, pat: str) -> list[dict]:
    """Return open issues labeled 'bug', sorted ascending by number."""
    issues = await _gh_get_all(
        f"/repos/{repo}/issues",
        pat,
        state="open",
        labels="bug",
        sort="created",
        direction="asc",
    )
    # Filter out pull requests (GitHub issues endpoint returns PRs too).
    return [i for i in issues if "pull_request" not in i]


async def _get_open_dev_prs(repo: str, pat: str) -> list[dict]:
    """Return open PRs whose head branch starts with a dev-worker prefix."""
    prs = await _gh_get_all(f"/repos/{repo}/pulls", pat, state="open")
    return [pr for pr in prs if (pr.get("head") or {}).get("ref", "").startswith(DEV_WORKER_BRANCH_PREFIX)]


async def _get_pr_files(repo: str, pr_num: int, pat: str) -> list[str]:
    """Return the full list of file paths changed in a PR (paginated).

    The files endpoint caps each page at 100 entries; PRs touching more
    files than that spill onto later pages. This feeds the ``triage``
    deny-list gate, so a truncated list here can hide a sensitive path
    beyond page 1 and mislabel the PR ``low_risk`` — keep paging until a
    short page confirms there's nothing left.
    """
    filenames: list[str] = []
    page = 1
    while True:
        resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}/files", pat, per_page=100, page=page)
        if resp.status_code == 401:
            raise _TokenError(f"GitHub 401 fetching files for PR #{pr_num}")
        resp.raise_for_status()
        batch: list[dict] = resp.json()
        filenames.extend(f["filename"] for f in batch)
        if len(batch) < 100:
            break
        page += 1
    return filenames


async def _get_pr_for_issue(repo: str, issue_num: int, pat: str) -> dict | None:
    """Return the first open PR that references this issue number, or None."""
    prs = await _get_open_dev_prs(repo, pat)
    for pr in prs:
        body = pr.get("body") or ""
        title = pr.get("title") or ""
        # Match common "Fixes #NNN" / "Closes #NNN" / "Resolves #NNN" patterns.
        if re.search(rf"\b(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#{issue_num}\b", body + " " + title, re.IGNORECASE):
            return pr
    return None


async def _get_issue(repo: str, issue_num: int, pat: str) -> dict | None:
    """Return the issue payload for ``issue_num``, or None if it doesn't exist.

    Used to distinguish "worker hasn't opened a PR yet" (issue still open)
    from "PR already merged and closed the issue" (terminal) when
    ``_get_pr_for_issue`` finds no open PR — a closed *and merged* PR is no
    longer in the open-PR search space it covers.
    """
    resp = await _gh_get(f"/repos/{repo}/issues/{issue_num}", pat)
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 fetching issue #{issue_num}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _get_pr_details(repo: str, pr_num: int, pat: str) -> dict:
    resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}", pat)
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 fetching PR #{pr_num}")
    resp.raise_for_status()
    return resp.json()


async def _get_check_runs(repo: str, head_sha: str, pat: str) -> dict[str, dict]:
    """Return latest check run per name (keyed by name)."""
    all_runs: list[dict] = []
    page = 1
    while True:
        resp = await _gh_get(f"/repos/{repo}/commits/{head_sha}/check-runs", pat, per_page=100, page=page)
        if resp.status_code == 401:
            raise _TokenError(f"GitHub 401 fetching check-runs for {head_sha}")
        resp.raise_for_status()
        batch: list[dict] = resp.json().get("check_runs", [])
        all_runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    latest: dict[str, dict] = {}
    for run in all_runs:
        name = run["name"]
        if name not in latest or run.get("id", 0) > latest[name].get("id", 0):
            latest[name] = run
    return latest


async def _ci_green(repo: str, head_sha: str, pat: str) -> bool:
    """True when all required CI checks passed for ``head_sha``."""
    latest = await _get_check_runs(repo, head_sha, pat)
    passed = {name for name, run in latest.items() if run.get("conclusion") == "success"}
    return REQUIRED_CHECKS.issubset(passed)


async def _apply_auto_merge_label(repo: str, pr_num: int, pat: str) -> bool:
    """Apply the ``auto-merge`` label so merge_queue.py picks the PR up.

    This is the loop's terminal action on the safe path — it hands the PR to the
    single merger (the queue) rather than merging here. Idempotent: GitHub's
    add-labels endpoint is a no-op if the label is already present.
    """
    resp = await _gh_post(
        f"/repos/{repo}/issues/{pr_num}/labels",
        pat,
        {"labels": [AUTO_MERGE_LABEL]},
    )
    if resp.status_code in (200, 201):
        return True
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 labelling PR #{pr_num}")
    logger.warning(
        "auto_dispatch: applying %s label returned %s for PR #%s",
        AUTO_MERGE_LABEL,
        resp.status_code,
        pr_num,
    )
    return False


# ---------------------------------------------------------------------------
# Eligibility selector
# ---------------------------------------------------------------------------


def _has_ac_block(issue_body: str) -> bool:
    """True when the issue body contains an ``## Acceptance Criteria`` section."""
    return bool(AC_SECTION_RE.search(issue_body or ""))


async def pick_next_candidate(
    repo: str,
    pat: str,
    *,
    in_flight_issue_nums: set[int] | None = None,
) -> tuple[dict | None, dict]:
    """Return ``(next_eligible_issue | None, skip_summary)``.

    ``skip_summary`` has keys:
    - ``total_bugs``: total open bug issues found.
    - ``skip_counts``: mapping of skip reason to count of issues skipped for
      that reason.  Keys are ``"in_flight"``, ``"no_ac_block"`` and
      ``"triage_hold"``.

    Eligible: open, labeled bug, has ``## Acceptance Criteria`` block,
    not already dispatched/in-flight, and not held by pre-dispatch triage
    (e.g. security-sensitive issues, which route to the manual lane).  A
    triage-held issue is skipped rather than short-circuiting the tick, so a
    security bug never head-of-line-blocks the ready bugs behind it.  Skipped
    issues are logged with reason.
    """
    issues = await _get_open_bug_issues(repo, pat)
    in_flight = in_flight_issue_nums or set()
    skip_counts: dict[str, int] = {}

    for issue in issues:
        issue_num = issue["number"]
        body = issue.get("body") or ""

        if issue_num in in_flight:
            logger.debug("auto_dispatch: skip issue #%s — already in flight", issue_num)
            skip_counts["in_flight"] = skip_counts.get("in_flight", 0) + 1
            continue

        if not _has_ac_block(body):
            logger.info("auto_dispatch: skip issue #%s — no AC block", issue_num)
            skip_counts["no_ac_block"] = skip_counts.get("no_ac_block", 0) + 1
            continue

        triage_decision, triage_reason = _pre_dispatch_triage(issue)
        if triage_decision == "hold":
            logger.info(
                "auto_dispatch: skip issue #%s — triage hold (%s); routing to manual lane",
                issue_num,
                triage_reason,
            )
            skip_counts["triage_hold"] = skip_counts.get("triage_hold", 0) + 1
            continue

        logger.info("auto_dispatch: candidate issue #%s — %s", issue_num, issue.get("title", ""))
        return issue, {"total_bugs": len(issues), "skip_counts": skip_counts}

    return None, {"total_bugs": len(issues), "skip_counts": skip_counts}


# ---------------------------------------------------------------------------
# Verdict helper
# ---------------------------------------------------------------------------


_warned_no_approvers = False


def _resolve_approvers() -> frozenset[str]:
    """GitHub logins whose verdict comments count, from AUTO_DISPATCH_APPROVERS.

    Empty setting → empty set → every verdict is ignored (fail-safe, per the
    configurable-agents decision: no operator login ships as a code default).
    A loud warning fires once per boot so the gap is visible in logs.
    """
    global _warned_no_approvers
    raw = settings.get("AUTO_DISPATCH_APPROVERS") or ""
    approvers = frozenset(login.strip() for login in raw.split(",") if login.strip())
    if not approvers and not _warned_no_approvers:
        _warned_no_approvers = True
        logger.warning(
            "AUTO_DISPATCH_APPROVERS is unset — ALL 'verdict:' PR comments are ignored. "
            "Set it on the /config page (e.g. your GitHub login) to enable the verdict gate."
        )
    return approvers


async def _get_verdict_from_pr(repo: str, pr_num: int, pat: str, approvers: frozenset[str]) -> str | None:
    """Return ``'pass'``, ``'fail'``, or None (no verdict posted yet).

    We look for a structured verdict comment posted by one of the configured
    ``approvers`` (AUTO_DISPATCH_APPROVERS setting). The comment must contain
    a line matching ``verdict: pass`` or ``verdict: fail`` (case-insensitive).
    An empty approver set means no comment can ever match (fail-safe).
    """
    if not approvers:
        return None
    comments = await _gh_get_all(f"/repos/{repo}/issues/{pr_num}/comments", pat)
    verdict_re = re.compile(r"^verdict:\s*(pass|fail)", re.IGNORECASE | re.MULTILINE)
    for comment in reversed(comments):
        user_login = (comment.get("user") or {}).get("login", "")
        if user_login not in approvers:
            continue
        m = verdict_re.search(comment.get("body") or "")
        if m:
            return m.group(1).lower()
    return None
