"""Epic orchestrator primitives (#751).

``dag`` — the sub-issue dependency DAG builder (#754): parses an epic's
task-list + each child's ``Depends-on:`` lines into a dependency graph,
detects cycles, and computes which children are ready to dispatch (every
parent merged to main). Pure and read-only.

``loop`` — the Stage 1 tick (#755): behind the ``EPIC_ORCHESTRATOR`` flag,
walks a configured epic's DAG and posts a per-issue approval card for each
ready child. No auto-dispatch, no merge, no deploy — those are later
stages (#756-#758).

This ``__init__`` re-exports the full public surface (the scheduler
resolves ``router.epic:tick`` against it), mirroring ``router.auto_dispatch``.
"""

from router.epic.config import (
    CALLABLE_REF,
    DEFAULT_PERIOD_SECONDS,
    DEFAULT_STATE_PATH,
    EPIC_PAT_PATH,
    TASK_NAME,
    load_epic_config,
)
from router.epic.dag import (
    DEFAULT_BASE_BRANCH,
    DagCycleError,
    DagError,
    build_dag,
    ready_nodes,
)
from router.epic.github import TokenError, _apply_epic_label, _get_issue, _get_open_pr_for_issue
from router.epic.loop import (
    _default_create_draft_fn,
    _dispatch_ready_child,
    _epic_label,
    _process_epic,
    _process_ready_child,
    _reconcile_landed_pr,
    register_epic_orchestrator,
    tick,
)
from router.epic.state import _mark_dispatched, _read_dispatched, _remove_dispatched, _state_path

__all__ = [
    "CALLABLE_REF",
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_PERIOD_SECONDS",
    "DEFAULT_STATE_PATH",
    "EPIC_PAT_PATH",
    "TASK_NAME",
    "DagCycleError",
    "DagError",
    "TokenError",
    "_apply_epic_label",
    "_default_create_draft_fn",
    "_dispatch_ready_child",
    "_epic_label",
    "_get_issue",
    "_get_open_pr_for_issue",
    "_mark_dispatched",
    "_process_epic",
    "_process_ready_child",
    "_read_dispatched",
    "_reconcile_landed_pr",
    "_remove_dispatched",
    "_state_path",
    "build_dag",
    "load_epic_config",
    "ready_nodes",
    "register_epic_orchestrator",
    "tick",
]
