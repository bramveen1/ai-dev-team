"""Unit tests for router.epic.dag — sub-issue dependency DAG builder (#754).

Fixture-driven: a linear chain (101 -> 102 -> 103), a diamond (201 -> 202,
203 -> 204), and a self/mutual cycle. All ``gh`` calls are stubbed via the
``run=`` injection point on ``gh_cli.run_gh``/``run_gh_json`` (#736) — no
real subprocess, no network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from router.epic.dag import (
    DagCycleError,
    DagError,
    build_dag,
    ready_nodes,
)

pytestmark = pytest.mark.unit


def _completed(stdout: Any = "", returncode: int = 0) -> SimpleNamespace:
    text = json.dumps(stdout) if not isinstance(stdout, str) else stdout
    return SimpleNamespace(stdout=text, stderr="", returncode=returncode)


class FakeGh:
    """Routes ``gh issue view``/``gh pr list`` invocations to canned fixtures.

    ``issue_bodies`` maps issue number -> body text.
    ``merged_prs`` maps parent issue number -> list of merged-PR json dicts
    (as ``gh pr list --json ...`` would return) to serve for that search.
    """

    def __init__(self, issue_bodies: dict[int, str], merged_prs: dict[int, list[dict]] | None = None):
        self.issue_bodies = issue_bodies
        self.merged_prs = merged_prs or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            number = int(cmd[3])
            if number not in self.issue_bodies:
                return _completed(returncode=1)
            return _completed({"body": self.issue_bodies[number]})
        if cmd[:3] == ["gh", "pr", "list"]:
            search = cmd[cmd.index("--search") + 1]
            parent_number = int(search.split()[0])
            return _completed(self.merged_prs.get(parent_number, []))
        raise AssertionError(f"unexpected gh invocation: {cmd!r}")


def _merged_pr(closes: int, base: str = "main", merged: bool = True) -> dict:
    return {
        "number": 900 + closes,
        "title": f"Fixes #{closes}",
        "body": "",
        "baseRefName": base,
        "mergedAt": "2026-07-20T00:00:00Z" if merged else None,
    }


# ---------------------------------------------------------------------------
# Fixture epics
# ---------------------------------------------------------------------------

# Linear chain: epic 100 lists 101, 102, 103; 102 depends on 101; 103 depends on 102.
LINEAR_EPIC_BODY = "## Sub-issues\n- [ ] #101\n- [ ] #102\n- [ ] #103\n"
LINEAR_BODIES = {
    100: LINEAR_EPIC_BODY,
    101: "No dependencies here.",
    102: "Some text.\nDepends-on: #101\n",
    103: "Depends-on: #102\n",
}

# Diamond: epic 200 lists 201, 202, 203, 204; 202 and 203 depend on 201; 204 depends on both.
DIAMOND_EPIC_BODY = "- [ ] #201\n- [ ] #202\n- [ ] #203\n- [ ] #204\n"
DIAMOND_BODIES = {
    200: DIAMOND_EPIC_BODY,
    201: "root, no deps",
    202: "Depends-on: #201\n",
    203: "Depends-on: #201\n",
    204: "Depends-on: #202, #203\n",
}

# Cycle: epic 300 lists 301, 302, 303; 301 <- 303 <- 302 <- 301.
CYCLE_EPIC_BODY = "- [ ] #301\n- [ ] #302\n- [ ] #303\n"
CYCLE_BODIES = {
    300: CYCLE_EPIC_BODY,
    301: "Depends-on: #303\n",
    302: "Depends-on: #301\n",
    303: "Depends-on: #302\n",
}


class TestBuildDagPositive:
    def test_linear_chain_edge_set(self):
        gh = FakeGh(LINEAR_BODIES)
        dag = build_dag(100, "o/r", run=gh)
        assert dag == {101: [], 102: [101], 103: [102]}

    def test_diamond_edge_set(self):
        gh = FakeGh(DIAMOND_BODIES)
        dag = build_dag(200, "o/r", run=gh)
        assert dag == {201: [], 202: [201], 203: [201], 204: [202, 203]}

    def test_task_list_ignores_checked_state(self):
        bodies = {100: "- [x] #101\n- [ ] #102\n", 101: "", 102: ""}
        dag = build_dag(100, "o/r", run=FakeGh(bodies))
        assert set(dag) == {101, 102}

    def test_dedupes_repeated_task_list_entries(self):
        bodies = {100: "- [ ] #101\n- [ ] #101\n", 101: ""}
        dag = build_dag(100, "o/r", run=FakeGh(bodies))
        assert dag == {101: []}

    def test_dedupes_repeated_depends_on_refs(self):
        bodies = {100: "- [ ] #101\n- [ ] #102\n", 101: "", 102: "Depends-on: #101, #101\n"}
        dag = build_dag(100, "o/r", run=FakeGh(bodies))
        assert dag == {101: [], 102: [101]}

    def test_external_dependency_kept_as_edge_not_as_node(self):
        """A Depends-on referencing an issue outside the epic's task list is
        kept as an edge (for readiness gating) but never becomes a DAG node."""
        bodies = {100: "- [ ] #101\n", 101: "Depends-on: #999\n"}
        dag = build_dag(100, "o/r", run=FakeGh(bodies))
        assert dag == {101: [999]}
        assert 999 not in dag


class TestBuildDagNegative:
    def test_cyclic_epic_raises_and_never_yields_a_dag(self):
        gh = FakeGh(CYCLE_BODIES)
        with pytest.raises(DagCycleError):
            build_dag(300, "o/r", run=gh)

    def test_self_dependency_raises(self):
        bodies = {100: "- [ ] #101\n", 101: "Depends-on: #101\n"}
        with pytest.raises(DagCycleError):
            build_dag(100, "o/r", run=FakeGh(bodies))

    def test_missing_epic_issue_raises_dag_error(self):
        gh = FakeGh({})
        with pytest.raises(DagError):
            build_dag(100, "o/r", run=gh)

    def test_missing_child_issue_raises_dag_error(self):
        gh = FakeGh({100: "- [ ] #101\n"})
        with pytest.raises(DagError):
            build_dag(100, "o/r", run=gh)

    def test_no_side_effects_only_reads(self):
        """Every gh invocation made while building the DAG must be a read."""
        gh = FakeGh(DIAMOND_BODIES)
        build_dag(200, "o/r", run=gh)
        for cmd in gh.calls:
            assert cmd[:3] == ["gh", "issue", "view"]
            assert "--json" in cmd


class TestReadyNodes:
    def test_root_nodes_always_ready(self):
        dag = {101: []}
        assert ready_nodes(dag, "o/r", run=FakeGh({})) == [101]

    def test_child_excluded_while_parent_pr_open(self):
        dag = {101: [], 102: [101]}
        gh = FakeGh({}, merged_prs={101: []})  # no merged PR found yet
        assert ready_nodes(dag, "o/r", run=gh) == [101]

    def test_child_included_once_parent_pr_merged(self):
        dag = {101: [], 102: [101]}
        gh = FakeGh({}, merged_prs={101: [_merged_pr(101)]})
        assert sorted(ready_nodes(dag, "o/r", run=gh)) == [101, 102]

    def test_diamond_child_needs_all_parents_merged(self):
        dag = {201: [], 202: [201], 203: [201], 204: [202, 203]}
        # Only 201 and 202 merged; 203 still open -> 204 must stay held.
        gh = FakeGh({}, merged_prs={201: [_merged_pr(201)], 202: [_merged_pr(202)], 203: []})
        assert sorted(ready_nodes(dag, "o/r", run=gh)) == [201, 202, 203]

    def test_diamond_child_ready_once_all_parents_merged(self):
        dag = {201: [], 202: [201], 203: [201], 204: [202, 203]}
        gh = FakeGh(
            {},
            merged_prs={201: [_merged_pr(201)], 202: [_merged_pr(202)], 203: [_merged_pr(203)]},
        )
        assert sorted(ready_nodes(dag, "o/r", run=gh)) == [201, 202, 203, 204]

    def test_merged_pr_into_wrong_base_branch_does_not_count(self):
        dag = {101: [], 102: [101]}
        gh = FakeGh({}, merged_prs={101: [_merged_pr(101, base="staging")]})
        assert ready_nodes(dag, "o/r", run=gh) == [101]

    def test_pr_without_closes_reference_does_not_count(self):
        dag = {101: [], 102: [101]}
        stray_pr = {"number": 1, "title": "unrelated", "body": "", "baseRefName": "main", "mergedAt": "x"}
        gh = FakeGh({}, merged_prs={101: [stray_pr]})
        assert ready_nodes(dag, "o/r", run=gh) == [101]

    def test_gh_failure_is_biased_to_hold(self):
        def boom(cmd, **kwargs):
            raise FileNotFoundError("gh not found")

        dag = {101: [], 102: [101]}
        assert ready_nodes(dag, "o/r", run=boom) == [101]

    def test_no_side_effects_only_reads(self):
        dag = {101: [], 102: [101]}
        gh = FakeGh({}, merged_prs={101: [_merged_pr(101)]})
        ready_nodes(dag, "o/r", run=gh)
        for cmd in gh.calls:
            assert cmd[:3] == ["gh", "pr", "list"]
            assert cmd[3:5] == ["--repo", "o/r"] or "--repo" in cmd
            assert "merged" in cmd
