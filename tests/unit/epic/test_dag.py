"""Unit tests for router.epic.dag — sub-issue dependency DAG builder (#754).

Fixture-driven: a linear chain (101 -> 102 -> 103), a diamond (201 -> 202,
203 -> 204), and a self/mutual cycle. All GitHub reads are stubbed at the
shared ``router.github_api`` REST helpers (``gh_get`` / ``gh_get_all``) —
no ``gh`` subprocess, no network. The DAG builder no longer shells out to
``gh`` (the router container ships no ``gh`` binary), so these tests also
assert that no ``gh`` subprocess is ever spawned.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from router.epic.dag import (
    DagCycleError,
    DagError,
    _parent_merged,
    build_dag,
    ready_nodes,
)

pytestmark = pytest.mark.unit


def _resp(status_code: int, json_data: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: json_data if json_data is not None else {},
    )


class FakeGitHub:
    """Async stand-in for ``router.github_api`` ``gh_get`` / ``gh_get_all``.

    ``issue_bodies`` maps issue number -> body text (missing -> 404).
    ``merged_prs`` is the flat list of closed-PR dicts (REST shape) the
    pulls-list endpoint returns; ``_parent_merged`` scans it for a
    close-reference to the parent it is checking.
    """

    def __init__(self, issue_bodies: dict[int, str], merged_prs: list[dict] | None = None):
        self.issue_bodies = issue_bodies
        self.merged_prs = merged_prs or []
        self.get_paths: list[str] = []
        self.get_all_calls: list[tuple[str, dict]] = []

    async def gh_get(self, path: str, pat: str, **params: Any) -> SimpleNamespace:
        self.get_paths.append(path)
        number = int(path.rsplit("/", 1)[-1])
        if number not in self.issue_bodies:
            return _resp(404)
        return _resp(200, {"body": self.issue_bodies[number]})

    async def gh_get_all(self, path: str, pat: str, **params: Any) -> list[dict]:
        self.get_all_calls.append((path, params))
        return list(self.merged_prs)


def _merged_pr(closes: int, base: str = "main", merged: bool = True) -> dict:
    return {
        "number": 900 + closes,
        "title": f"Fixes #{closes}",
        "body": "",
        "base": {"ref": base},
        "merged_at": "2026-07-20T00:00:00Z" if merged else None,
    }


def _patch(fake: FakeGitHub):
    return (
        patch("router.epic.dag.github_api.gh_get", new=AsyncMock(side_effect=fake.gh_get)),
        patch("router.epic.dag.github_api.gh_get_all", new=AsyncMock(side_effect=fake.gh_get_all)),
    )


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


@pytest.mark.asyncio
class TestBuildDagPositive:
    async def test_linear_chain_edge_set(self):
        fake = FakeGitHub(LINEAR_BODIES)
        p1, p2 = _patch(fake)
        with p1, p2:
            dag = await build_dag(100, "o/r", "pat")
        assert dag == {101: [], 102: [101], 103: [102]}

    async def test_diamond_edge_set(self):
        fake = FakeGitHub(DIAMOND_BODIES)
        p1, p2 = _patch(fake)
        with p1, p2:
            dag = await build_dag(200, "o/r", "pat")
        assert dag == {201: [], 202: [201], 203: [201], 204: [202, 203]}

    async def test_task_list_ignores_checked_state(self):
        bodies = {100: "- [x] #101\n- [ ] #102\n", 101: "", 102: ""}
        fake = FakeGitHub(bodies)
        p1, p2 = _patch(fake)
        with p1, p2:
            dag = await build_dag(100, "o/r", "pat")
        assert set(dag) == {101, 102}

    async def test_dedupes_repeated_task_list_entries(self):
        bodies = {100: "- [ ] #101\n- [ ] #101\n", 101: ""}
        fake = FakeGitHub(bodies)
        p1, p2 = _patch(fake)
        with p1, p2:
            dag = await build_dag(100, "o/r", "pat")
        assert dag == {101: []}

    async def test_dedupes_repeated_depends_on_refs(self):
        bodies = {100: "- [ ] #101\n- [ ] #102\n", 101: "", 102: "Depends-on: #101, #101\n"}
        fake = FakeGitHub(bodies)
        p1, p2 = _patch(fake)
        with p1, p2:
            dag = await build_dag(100, "o/r", "pat")
        assert dag == {101: [], 102: [101]}

    async def test_external_dependency_kept_as_edge_not_as_node(self):
        """A Depends-on referencing an issue outside the epic's task list is
        kept as an edge (for readiness gating) but never becomes a DAG node."""
        bodies = {100: "- [ ] #101\n", 101: "Depends-on: #999\n"}
        fake = FakeGitHub(bodies)
        p1, p2 = _patch(fake)
        with p1, p2:
            dag = await build_dag(100, "o/r", "pat")
        assert dag == {101: [999]}
        assert 999 not in dag


@pytest.mark.asyncio
class TestBuildDagNegative:
    async def test_cyclic_epic_raises_and_never_yields_a_dag(self):
        fake = FakeGitHub(CYCLE_BODIES)
        p1, p2 = _patch(fake)
        with p1, p2, pytest.raises(DagCycleError):
            await build_dag(300, "o/r", "pat")

    async def test_self_dependency_raises(self):
        bodies = {100: "- [ ] #101\n", 101: "Depends-on: #101\n"}
        fake = FakeGitHub(bodies)
        p1, p2 = _patch(fake)
        with p1, p2, pytest.raises(DagCycleError):
            await build_dag(100, "o/r", "pat")

    async def test_missing_epic_issue_raises_dag_error(self):
        fake = FakeGitHub({})
        p1, p2 = _patch(fake)
        with p1, p2, pytest.raises(DagError):
            await build_dag(100, "o/r", "pat")

    async def test_missing_child_issue_raises_dag_error(self):
        fake = FakeGitHub({100: "- [ ] #101\n"})
        p1, p2 = _patch(fake)
        with p1, p2, pytest.raises(DagError):
            await build_dag(100, "o/r", "pat")

    async def test_transport_error_raises_dag_error(self):
        """A raw transport failure (e.g. httpx error) is folded into DagError."""
        with patch("router.epic.dag.github_api.gh_get", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(DagError):
                await build_dag(100, "o/r", "pat")

    async def test_build_dag_only_reads_issue_endpoints(self):
        """Every GitHub call made while building the DAG is an issue read —
        no PR mutation, and crucially no ``gh`` subprocess (helpers are HTTP)."""
        fake = FakeGitHub(DIAMOND_BODIES)
        p1, p2 = _patch(fake)
        with p1, p2:
            await build_dag(200, "o/r", "pat")
        assert fake.get_paths, "expected at least one issue read"
        for path in fake.get_paths:
            assert path.startswith("/repos/o/r/issues/")


@pytest.mark.asyncio
class TestReadyNodes:
    async def test_root_nodes_always_ready(self):
        fake = FakeGitHub({})
        p1, p2 = _patch(fake)
        with p1, p2:
            assert await ready_nodes({101: []}, "o/r", "pat") == [101]

    async def test_child_excluded_while_parent_pr_open(self):
        fake = FakeGitHub({}, merged_prs=[])  # no merged PR found yet
        p1, p2 = _patch(fake)
        with p1, p2:
            assert await ready_nodes({101: [], 102: [101]}, "o/r", "pat") == [101]

    async def test_child_included_once_parent_pr_merged(self):
        fake = FakeGitHub({}, merged_prs=[_merged_pr(101)])
        p1, p2 = _patch(fake)
        with p1, p2:
            got = await ready_nodes({101: [], 102: [101]}, "o/r", "pat")
        assert sorted(got) == [101, 102]

    async def test_diamond_child_needs_all_parents_merged(self):
        dag = {201: [], 202: [201], 203: [201], 204: [202, 203]}
        # Only 201 and 202 merged; 203 still open -> 204 must stay held.
        fake = FakeGitHub({}, merged_prs=[_merged_pr(201), _merged_pr(202)])
        p1, p2 = _patch(fake)
        with p1, p2:
            got = await ready_nodes(dag, "o/r", "pat")
        assert sorted(got) == [201, 202, 203]

    async def test_diamond_child_ready_once_all_parents_merged(self):
        dag = {201: [], 202: [201], 203: [201], 204: [202, 203]}
        fake = FakeGitHub({}, merged_prs=[_merged_pr(201), _merged_pr(202), _merged_pr(203)])
        p1, p2 = _patch(fake)
        with p1, p2:
            got = await ready_nodes(dag, "o/r", "pat")
        assert sorted(got) == [201, 202, 203, 204]

    async def test_merged_pr_into_wrong_base_branch_does_not_count(self):
        fake = FakeGitHub({}, merged_prs=[_merged_pr(101, base="staging")])
        p1, p2 = _patch(fake)
        with p1, p2:
            assert await ready_nodes({101: [], 102: [101]}, "o/r", "pat") == [101]

    async def test_pr_without_closes_reference_does_not_count(self):
        stray = {"number": 1, "title": "unrelated", "body": "", "base": {"ref": "main"}, "merged_at": "x"}
        fake = FakeGitHub({}, merged_prs=[stray])
        p1, p2 = _patch(fake)
        with p1, p2:
            assert await ready_nodes({101: [], 102: [101]}, "o/r", "pat") == [101]

    async def test_unmerged_closing_pr_does_not_count(self):
        fake = FakeGitHub({}, merged_prs=[_merged_pr(101, merged=False)])
        p1, p2 = _patch(fake)
        with p1, p2:
            assert await ready_nodes({101: [], 102: [101]}, "o/r", "pat") == [101]

    async def test_lookup_failure_is_biased_to_hold(self):
        with patch("router.epic.dag.github_api.gh_get_all", new=AsyncMock(side_effect=RuntimeError("boom"))):
            assert await ready_nodes({101: [], 102: [101]}, "o/r", "pat") == [101]

    async def test_ready_nodes_only_reads_pulls_endpoint(self):
        fake = FakeGitHub({}, merged_prs=[_merged_pr(101)])
        p1, p2 = _patch(fake)
        with p1, p2:
            await ready_nodes({101: [], 102: [101]}, "o/r", "pat")
        assert fake.get_all_calls, "expected a pulls read"
        for path, params in fake.get_all_calls:
            assert path == "/repos/o/r/pulls"
            assert params.get("state") == "closed"


@pytest.mark.asyncio
class TestParentMerged:
    async def test_passes_base_filter_to_pulls_list(self):
        fake = FakeGitHub({}, merged_prs=[_merged_pr(101)])
        p1, p2 = _patch(fake)
        with p1, p2:
            assert await _parent_merged("o/r", 101, "pat", base_branch="main") is True
        _, params = fake.get_all_calls[0]
        assert params.get("base") == "main"
