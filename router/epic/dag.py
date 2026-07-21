"""Sub-issue dependency DAG builder for the epic orchestrator (#754).

Pure, read-only: given an epic issue number, returns the sub-issue
dependency graph and which sub-issues are ready to dispatch. No dispatch,
merge, or write side effects — every ``gh`` call here is a read.

DAG source is GitHub itself (decision (a) in #751 — no committed manifest,
no new dependency):

- **Children** come from the epic body's task list (``- [ ] #123``).
- **Edges** come from each child's own body: a ``Depends-on: #x[, #y]``
  line names that child's parents.

Shells out via ``gh`` through ``packs/dispatch/gh_cli.py`` — the existing
centralized subprocess wrapper (#736) — rather than adding a new HTTP
client. Mirrors the ``sys.path`` injection already used by
``router/github_api.py`` and ``router/dispatch/supervision.py`` to reach
that pack-mounted module.
"""

from __future__ import annotations

import logging
import re
import sys as _sys
from pathlib import Path as _Path
from typing import Any

_PACK_DIR = _Path(__file__).resolve().parent.parent.parent / "packs" / "dispatch"
if _PACK_DIR.is_dir() and str(_PACK_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PACK_DIR))

from gh_cli import run_gh_json  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_BASE_BRANCH = "main"
DEFAULT_TIMEOUT_SECONDS = 30

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


def _issue_body(repo: str, issue_number: int, *, run: Any, timeout: float) -> str:
    result, data = run_gh_json(
        ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
        timeout=timeout,
        run=run,
    )
    if not result.ok or not isinstance(data, dict):
        raise DagError(f"failed to read issue #{issue_number}: {result.error_detail()}")
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


def build_dag(
    epic_number: int,
    repo: str,
    *,
    run: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[int, list[int]]:
    """Return ``{child_issue_number: [parent_issue_number, ...]}`` for *epic_number*.

    Children are parsed from the epic body's task list; each child's parents
    come from its own ``Depends-on:`` line(s) (parents outside the epic's
    task list are kept as edges but never become DAG nodes themselves).

    Raises :class:`DagCycleError` when the resulting graph is cyclic — a
    cyclic epic never yields a dag, so callers can never derive a ready set
    from one.
    """
    epic_body = _issue_body(repo, epic_number, run=run, timeout=timeout)
    children = _parse_children(epic_body)

    dag: dict[int, list[int]] = {}
    for child in children:
        child_body = _issue_body(repo, child, run=run, timeout=timeout)
        dag[child] = _parse_depends_on(child_body)

    _check_acyclic(dag)
    return dag


def _parent_merged(
    repo: str,
    parent_number: int,
    *,
    base_branch: str,
    run: Any,
    timeout: float,
) -> bool:
    """True only when a PR closing *parent_number* has merged into *base_branch*.

    Verified live via ``gh`` on every call (no caching). Biased to hold: a
    failed lookup, malformed response, or absence of a matching merged PR
    all return False rather than assuming readiness.
    """
    result, data = run_gh_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--search",
            f"{parent_number} in:body",
            "--json",
            "number,title,body,baseRefName,mergedAt",
        ],
        timeout=timeout,
        run=run,
    )
    if not result.ok or not isinstance(data, list):
        logger.warning(
            "epic.dag: could not verify merge state of parent #%s: %s",
            parent_number,
            result.error_detail(),
        )
        return False

    for pr in data:
        if pr.get("baseRefName") != base_branch or not pr.get("mergedAt"):
            continue
        haystack = f"{pr.get('title') or ''} {pr.get('body') or ''}"
        if parent_number in (int(ref) for ref in _CLOSES_REF_RE.findall(haystack)):
            return True
    return False


def ready_nodes(
    dag: dict[int, list[int]],
    repo: str,
    *,
    base_branch: str = DEFAULT_BASE_BRANCH,
    run: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[int]:
    """Return the children of *dag* whose every parent has merged to *base_branch*.

    Root nodes (an empty parent list) are always ready. Parent merge state
    is re-verified live via ``gh`` on every call — never cached, never
    inferred from the epic body alone.
    """
    ready: list[int] = []
    for child, parents in dag.items():
        if all(_parent_merged(repo, p, base_branch=base_branch, run=run, timeout=timeout) for p in parents):
            ready.append(child)
    return ready
