"""Autonomous bug-backlog loop (issue #535).

Drains the bug backlog at ~2/hr with zero human interaction on the safe path:

  eligible bug → triage gate → dispatch worker → Sam verdict →
    low-risk + pass + CI green → apply ``auto-merge`` label (or "would label"
                                 in shadow mode)
    sensitive | fail | red    → hold for human

This loop never merges. Its terminal action on the safe path is to apply the
``auto-merge`` label; the normal merge queue (``merge_queue.py``) is the single
merger and squash-merges the labelled, CI-green PR on a later tick. One merger,
one identity (``aidt-merge``), one place to reason about merge-time TOCTOU.

Design decisions (locked in issue #535; merge path removed per Bram, 2026-06):
- Eligibility: open bug issue with an ``## Acceptance Criteria`` block.
- Cadence: ≤ ``rate_per_hour`` dispatches/hr (default 2).
- Concurrency: one bug in flight at a time (zero open dev PRs + empty merge queue).
- Daily cap: ``daily_cap`` per day (default 6), persisted across restarts.
- Kill switch: ``auto_dispatch.enabled``, defaults off.
- Triage: path globs + changed-file count; bias to hold.
- Shadow mode: does everything except label; posts "would label".
- Merge: delegated to ``merge_queue.py`` (gate: ``auto-merge`` label + CI green
  + ``mergeable_state==clean``). SHA-pinned merge safety is tracked in #513,
  which the queue owns.

Package layout (split from the original single module):

- ``config``   — constants + ``load_auto_dispatch_config``.
- ``state``    — persisted JSON sidecars (counters, awaiting, pending
  approval, stall dedup).
- ``triage``   — machine-checkable triage gate (diff globs + issue prescan).
- ``github``   — GitHub reads/writes (issues, PRs, CI, verdicts, labels).
- ``notify``   — best-effort Slack posts.
- ``inflight`` — live dispatch-slot checks + orphan sweep.
- ``worker``   — dev-worker dispatch via docker exec.
- ``loop``     — the tick orchestration + verdict handling + registration.

This ``__init__`` re-exports the full public surface (the scheduler resolves
``router.auto_dispatch:tick`` against it), so external callers are unaffected
by the split.
"""

from router.auto_dispatch.config import (
    AC_SECTION_RE,
    AUTO_MERGE_LABEL,
    AWAITING_MAX_AGE_SECONDS,
    CALLABLE_REF,
    DEFAULT_COUNTER_PATH,
    DEFAULT_PERIOD_SECONDS,
    DEV_WORKER_BRANCH_PREFIX,
    MERGE_PAT_PATH,
    PENDING_APPROVAL_MAX_AGE_SECONDS,
    REQUIRED_CHECKS,
    TASK_NAME,
    load_auto_dispatch_config,
)
from router.auto_dispatch.github import (
    _apply_auto_merge_label,
    _auth_headers,
    _ci_green,
    _get_check_runs,
    _get_open_bug_issues,
    _get_open_dev_prs,
    _get_pr_details,
    _get_pr_files,
    _get_pr_for_issue,
    _get_verdict_from_pr,
    _gh_get,
    _gh_post,
    _gh_put,
    _has_ac_block,
    _read_pat,
    _resolve_approvers,
    _TokenError,
    pick_next_candidate,
)
from router.auto_dispatch.inflight import (
    _get_in_flight_issue_nums,
    _has_any_in_flight_dispatch,
    _run_periodic_orphan_sweep,
)
from router.auto_dispatch.loop import (
    _process_awaiting,
    _tick_impl,
    handle_pr_verdict,
    register_auto_dispatch,
    tick,
)
from router.auto_dispatch.notify import _slack_post, _slack_post_with_ts
from router.auto_dispatch.state import (
    _add_awaiting,
    _add_pending_approval,
    _awaiting_path,
    _current_hour_str,
    _pending_approval_path,
    _read_awaiting,
    _read_counters,
    _read_last_stall_state,
    _read_pending_approval,
    _remove_awaiting,
    _remove_pending_approval,
    _stall_state_path,
    _today_str,
    _write_awaiting,
    _write_counters,
    _write_last_stall_state,
    _write_pending_approval,
    get_counters,
    increment_counters,
)
from router.auto_dispatch.triage import (
    TRIAGE_DENY_GLOBS,
    _compile_glob,
    _path_matches_deny,
    _pre_dispatch_triage,
    triage,
)
from router.auto_dispatch.worker import _default_create_draft_fn, _dispatch_worker

__all__ = [
    "AC_SECTION_RE",
    "AUTO_MERGE_LABEL",
    "AWAITING_MAX_AGE_SECONDS",
    "CALLABLE_REF",
    "DEFAULT_COUNTER_PATH",
    "DEFAULT_PERIOD_SECONDS",
    "DEV_WORKER_BRANCH_PREFIX",
    "MERGE_PAT_PATH",
    "PENDING_APPROVAL_MAX_AGE_SECONDS",
    "REQUIRED_CHECKS",
    "TASK_NAME",
    "TRIAGE_DENY_GLOBS",
    "_TokenError",
    "_add_awaiting",
    "_add_pending_approval",
    "_apply_auto_merge_label",
    "_auth_headers",
    "_awaiting_path",
    "_ci_green",
    "_compile_glob",
    "_current_hour_str",
    "_default_create_draft_fn",
    "_dispatch_worker",
    "_get_check_runs",
    "_get_in_flight_issue_nums",
    "_get_open_bug_issues",
    "_get_open_dev_prs",
    "_get_pr_details",
    "_get_pr_files",
    "_get_pr_for_issue",
    "_get_verdict_from_pr",
    "_gh_get",
    "_gh_post",
    "_gh_put",
    "_has_ac_block",
    "_has_any_in_flight_dispatch",
    "_path_matches_deny",
    "_pending_approval_path",
    "_pre_dispatch_triage",
    "_process_awaiting",
    "_read_awaiting",
    "_read_counters",
    "_read_last_stall_state",
    "_read_pat",
    "_read_pending_approval",
    "_remove_awaiting",
    "_remove_pending_approval",
    "_resolve_approvers",
    "_run_periodic_orphan_sweep",
    "_slack_post",
    "_slack_post_with_ts",
    "_stall_state_path",
    "_tick_impl",
    "_today_str",
    "_write_awaiting",
    "_write_counters",
    "_write_last_stall_state",
    "_write_pending_approval",
    "get_counters",
    "handle_pr_verdict",
    "increment_counters",
    "load_auto_dispatch_config",
    "pick_next_candidate",
    "register_auto_dispatch",
    "tick",
    "triage",
]
