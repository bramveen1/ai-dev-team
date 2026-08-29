"""Sub-issue dependency DAG builder for the epic orchestrator (#754).

Pure, read-only: given an epic issue number, returns the sub-issue
dependency graph and which sub-issues are ready to dispatch. No dispatch,
merge, or write side effects — every GitHub call here is a read.

DAG source is GitHub itself (decision (a) in #751 — no committed manifest,
no new dependency):

- **Children** come from the epic body's task list (``- [ ] #123``).
- **Edges** come from each child's own body: a ``Depends-on: #x[, #y]``
  line names that child's parents.

Reads go through the shared authenticated ``httpx`` helpers in
``router.github_api`` (the same path ``auto_dispatch`` and ``merge_queue``
use), **not** a ``gh`` subprocess. The router container ships no ``gh``
binary, so the previous ``gh``-shell-out DAG build failed at
``gh binary not found`` on every tick, silently yielding an empty ready set
(the ``EPIC_ORCHESTRATOR`` heartbeat surfaced it, #846). Using the REST
helpers also keeps these calls non-blocking on the router's event loop,
unlike the old blocking ``subprocess`` call.
"""

from __future__ import annotations

import logging
import re

from router import github_api

logger = logging.getLogger(__name__)

TokenError = github_api.TokenError

DEFAULT_BASE_BRANCH = "main"

# `- [ ] #123` / `- [x] #123` task-list entries in an epic body.
_TASK_LIST_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*#(\d+)", re.MULTILINE)
# `Depends-on: #x, #y` lines in a sub-issue body.
_DEPENDS_ON_RE = re.compile(r"^\s*Depends-on:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#(\d+)")
# `Fixes #N` / `Closes #N` / `Resolves #N` (+ -es/-ed variants), the GitHub
# auto-close convention, used to find the PR that closed a parent issue.
_CLOSES_REF_RE = re.compile(r"\b(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)

__all__ = [
    "DEFAULT_BASE_BRANCH",
    "DagCycleError",
    "DagError",
    "build_dag",
    "ready_nodes",
]


class DagError(Exception):
    """Base class for DAG build failures."""


class DagCycleError(DagError):
    """Raised when the sub-issue dependency graph contains a cycle.

    ``cycle`` is the offending path, e.g. ``[101, 102, 103, 101]``.
    """

    def __init__(self, cycle: list[int]):
        self.cycle = cycle
        path = " -> ".join(f"#{n}" for n in cycle)
        super().__init__(f"dependency cycle detected: {path}")


async def _issue_body(repo: str, issue_number: int, pat: str) -> str:
    """Return the body text of *issue_number*, read via the REST helper.

    Any non-200 (including a 401, which the shared helper would otherwise
    escalate as ``TokenError``) is folded into :class:`DagError` so the
    orchestrator loop's existing ``DagError`` handler owns every DAG-build
    failure — a build that can't read an issue never yields a partial DAG.
    """
    try:
        resp = await github_api.gh_get(f"/repos/{repo}/issues/{issue_number}", pat)
    except Exception as exc:  # noqa: BLE001 — any transport error is a build failure
        raise DagError(f"failed to read issue #{issue_number}: {exc}") from exc
    if resp.status_code != 200:
        raise DagError(f"failed to read issue #{issue_number}: GitHub API status {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise DagError(f"failed to read issue #{issue_number}: unexpected response shape")
    return data.get("body") or ""


def _parse_children(epic_body: str) -> list[int]:
    children: list[int] = []
    for m in _TASK_LIST_RE.finditer(epic_body):
        n = int(m.group(1))
        if n not in children:
            children.append(n)
    return children


def _parse_depends_on(issue_body: str) -> list[int]:
    parents: list[int] = []
    for line_match in _DEPENDS_ON_RE.finditer(issue_body):
        for ref_match in _ISSUE_REF_RE.finditer(line_match.group(1)):
            n = int(ref_match.group(1))
            if n not in parents:
                parents.append(n)
    return parents


def _check_acyclic(dag: dict[int, list[int]]) -> None:
    """Raise :class:`DagCycleError` if following parent edges revisits a node."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = dict.fromkeys(dag, WHITE)
    path: list[int] = []

    def visit(node: int) -> None:
        color[node] = GRAY
        path.append(node)
        for parent in dag.get(node, []):
            if parent not in dag:
                continue  # external dependency, not itself a DAG node
            state = color.get(parent, WHITE)
            if state == GRAY:
                cycle_start = path.index(parent)
                raise DagCycleError([*path[cycle_start:], parent])
            if state == WHITE:
                visit(parent)
        path.pop()
        color[node] = BLACK

    for node in list(dag):
        if color[node] == WHITE:
            visit(node)


async def build_dag(epic_number: int, repo: str, pat: str) -> dict[int, list[int]]:
    """Return ``{child_issue_number: [parent_issue_number, ...]}`` for *epic_number*.

    Children are parsed from the epic body's task list; each child's parents
    come from its own ``Depends-on:`` line(s) (parents outside the epic's
    task list are kept as edges but never become DAG nodes themselves).

    Raises :class:`DagCycleError` when the resulting graph is cyclic — a
    cyclic epic never yields a dag, so callers can never derive a ready set
    from one — and :class:`DagError` if any issue read fails.
    """
    epic_body = await _issue_body(repo, epic_number, pat)
    children = _parse_children(epic_body)

    dag: dict[int, list[int]] = {}
    for child in children:
        child_body = await _issue_body(repo, child, pat)
        dag[child] = _parse_depends_on(child_body)

    _check_acyclic(dag)
    return dag


async def _parent_merged(repo: str, parent_number: int, pat: str, *, base_branch: str) -> bool:
    """True only when a PR closing *parent_number* has merged into *base_branch*.

    Verified live via the REST pulls list (filtered to ``state=closed`` and
    ``base=base_branch`` server-side) on every call — no caching. Biased to
    hold: a failed lookup, malformed response, or absence of a matching
    merged PR all return False rather than assuming readiness.
    """
    try:
        prs = await github_api.gh_get_all(f"/repos/{repo}/pulls", pat, state="closed", base=base_branch)
    except Exception as exc:  # noqa: BLE001 — bias to hold on any lookup failure
        logger.warning(
            "epic.dag: could not verify merge state of parent #%s: %s",
            parent_number,
            exc,
        )
        return False

    if not isinstance(prs, list):
        logger.warning("epic.dag: unexpected pulls response verifying parent #%s", parent_number)
        return False

    for pr in prs:
        base_ref = (pr.get("base") or {}).get("ref")
        if base_ref != base_branch or not pr.get("merged_at"):
            continue
        haystack = f"{pr.get('title') or ''} {pr.get('body') or ''}"
        if parent_number in (int(ref) for ref in _CLOSES_REF_RE.findall(haystack)):
            return True
    return False


async def ready_nodes(
    dag: dict[int, list[int]],
    repo: str,
    pat: str,
    *,
    base_branch: str = DEFAULT_BASE_BRANCH,
) -> list[int]:
    """Return the children of *dag* whose every parent has merged to *base_branch*.

    Root nodes (an empty parent list) are always ready. Parent merge state
    is re-verified live via the REST API on every call — never cached, never
    inferred from the epic body alone.
    """
    ready: list[int] = []
    for child, parents in dag.items():
        all_merged = True
        for parent in parents:
            if not await _parent_merged(repo, parent, pat, base_branch=base_branch):
                all_merged = False
                break
        if all_merged:
            ready.append(child)
    return ready
