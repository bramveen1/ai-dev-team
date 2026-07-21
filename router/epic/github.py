"""GitHub reads/writes for the epic orchestrator loop (#755).

Thin wrappers over ``router.github_api`` (shared with ``auto_dispatch`` and
``merge_queue``): fetching an issue's title/URL, finding its open PR,
checking whether a child has reached a terminal state (closed issue /
merged closing PR — #768), and applying the ``epic:<slug>`` label. The
read-only DAG traversal (#754) lives in ``router.epic.dag`` and is
untouched here — this module only adds the live lookups Stage 1 needs
beyond that.
"""

from __future__ import annotations

import logging
import re

from router import github_api

logger = logging.getLogger(__name__)

TokenError = github_api.TokenError
_gh_get = github_api.gh_get
_gh_post = github_api.gh_post

# `Fixes #N` / `Closes #N` / `Resolves #N` (+ -es/-ed variants), the GitHub
# auto-close convention, used to match a PR to the issue it closes.
_CLOSES_REF_RE = re.compile(r"\b(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)


async def _get_issue(repo: str, issue_num: int, pat: str) -> dict | None:
    """Return the issue payload (title, html_url, ...), or None if unreadable."""
    resp = await _gh_get(f"/repos/{repo}/issues/{issue_num}", pat)
    if resp.status_code == 401:
        raise TokenError(f"GitHub 401 fetching issue #{issue_num}")
    if resp.status_code != 200:
        logger.warning("epic_orchestrator: could not fetch issue #%s (status=%s)", issue_num, resp.status_code)
        return None
    return resp.json()


def _pr_closes_issue(pr: dict, issue_num: int) -> bool:
    haystack = f"{pr.get('title') or ''} {pr.get('body') or ''}"
    return issue_num in (int(ref) for ref in _CLOSES_REF_RE.findall(haystack))


async def _get_open_pr_for_issue(repo: str, issue_num: int, pat: str) -> dict | None:
    """Return the first open PR whose title/body closes *issue_num*, or None."""
    resp = await _gh_get(f"/repos/{repo}/pulls", pat, state="open", per_page=100)
    if resp.status_code == 401:
        raise TokenError(f"GitHub 401 listing PRs for {repo}")
    resp.raise_for_status()
    for pr in resp.json():
        if _pr_closes_issue(pr, issue_num):
            return pr
    return None


async def _has_merged_closing_pr(repo: str, issue_num: int, pat: str) -> bool:
    """True if any *closed* PR that closes *issue_num* actually merged."""
    resp = await _gh_get(f"/repos/{repo}/pulls", pat, state="closed", per_page=100)
    if resp.status_code == 401:
        raise TokenError(f"GitHub 401 listing closed PRs for {repo}")
    resp.raise_for_status()
    return any(pr.get("merged_at") and _pr_closes_issue(pr, issue_num) for pr in resp.json())


async def _is_child_terminal(repo: str, issue_num: int, pat: str) -> bool:
    """True once *issue_num* has reached a terminal state: closed issue or merged closing PR.

    Gates dispatch at the root cause (#768): the dispatched-tracker entry
    for a child is cleared as soon as its PR lands (``_reconcile_landed_pr``
    in ``router.epic.loop``, so the sidecar doesn't grow unbounded while a
    human reviews it) — well before the PR merges. Without this check that
    tracker-clear reopens the dispatch window the instant the open-PR
    branch in ``_process_ready_child`` no longer matches, causing a fresh
    approval card to be posted for already-merged work on every tick.
    """
    issue = await _get_issue(repo, issue_num, pat)
    if issue is not None and issue.get("state") == "closed":
        return True
    return await _has_merged_closing_pr(repo, issue_num, pat)


async def _apply_epic_label(repo: str, pr_num: int, label: str, pat: str) -> bool:
    """Idempotently apply *label* (e.g. ``epic:foo``) to a PR/issue.

    GitHub's add-labels endpoint is a no-op if the label is already present.
    """
    resp = await _gh_post(f"/repos/{repo}/issues/{pr_num}/labels", pat, {"labels": [label]})
    if resp.status_code in (200, 201):
        return True
    if resp.status_code == 401:
        raise TokenError(f"GitHub 401 labelling PR #{pr_num}")
    logger.warning(
        "epic_orchestrator: applying label %r returned %s for PR #%s",
        label,
        resp.status_code,
        pr_num,
    )
    return False
