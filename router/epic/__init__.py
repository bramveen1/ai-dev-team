"""Epic orchestrator primitives (#751).

``dag`` — the sub-issue dependency DAG builder (#754): parses an epic's
task-list + each child's ``Depends-on:`` lines into a dependency graph,
detects cycles, and computes which children are ready to dispatch (every
parent merged to main). Pure and read-only; the loop that consumes it
(dispatch ordering, cadence caps) is out of scope here (#755+).
"""

from router.epic.dag import (
    DEFAULT_BASE_BRANCH,
    DagCycleError,
    DagError,
    build_dag,
    ready_nodes,
)

__all__ = [
    "DEFAULT_BASE_BRANCH",
    "DagCycleError",
    "DagError",
    "build_dag",
    "ready_nodes",
]
