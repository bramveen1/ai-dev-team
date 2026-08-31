"""Constants and config loading for the epic orchestrator loop (#755).

Mirrors ``router/auto_dispatch/config.py``'s pattern: every tunable lives
here, ``config/epic.yaml`` is re-read on every tick so adding/removing a
tracked epic takes effect without a restart. The on/off switch itself is
the ``EPIC_ORCHESTRATOR`` settings flag (hot-reload, ``router/settings.py``)
— this module only supplies content (repo, tracked epics, dispatch
defaults), not the kill switch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from router import github_api

logger = logging.getLogger(__name__)

CALLABLE_REF = "router.epic:tick"
TASK_NAME = "epic-orchestrator-loop"

# Default cadence for the scheduler system task.
DEFAULT_PERIOD_SECONDS = 1800

# Where we persist the "already dispatched" tracker so restarts don't
# re-post an approval card for a child that's still awaiting a human click.
DEFAULT_STATE_PATH = "/var/lib/dispatch/_epic_orchestrator_state.json"

# TTL age-out (#854) for a tracker entry with no landed PR: a worker that
# crashes (auth failure, timeout, ...) before opening a PR leaves an entry
# that `_reconcile_landed_pr`'s clear-on-landing path never sees, wedging
# re-dispatch forever. Mirrors `auto_dispatch.config.AWAITING_MAX_AGE_SECONDS`'s
# pattern (age-out on the dispatch timestamp), sized at 60 minutes: the
# worker dispatch timeout is always 30 minutes, so a 60-minute floor cannot
# race a still-running worker — anything older is definitively dead.
EPIC_DISPATCH_MAX_AGE_SECONDS = 60 * 60

DEFAULT_BASE_BRANCH = "main"

# Reuses the aidt-merge PAT for both reads (issues/PRs) and the one write
# (applying the epic:<slug> label) — same rationale as
# auto_dispatch/config.py's MERGE_PAT_PATH: no new secret introduced.
EPIC_PAT_PATH = github_api.MERGE_PAT_PATH


def load_epic_config(config_path: str | None = None) -> dict:
    """Load the ``epic_orchestrator`` block from ``config/epic.yaml``.

    Returns a dict with all keys present (defaults filled in). The caller
    should not cache this — re-read on every tick so config changes take
    effect without a restart.
    """
    defaults: dict[str, Any] = {
        "repo": "",
        "base_branch": DEFAULT_BASE_BRANCH,
        "epics": [],
        "worker_agent": None,
        "worker_model": "sonnet",
        "worker_persona": "dev",
        "worker_budget_seconds": 1800,
    }

    if config_path is None:
        # config/epic.yaml relative to the repo root (two levels up from
        # this file: epic/ → router/ → repo root).
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "epic.yaml")

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        block = raw.get("epic_orchestrator") or {}
        cfg = {**defaults, **block}
    except FileNotFoundError:
        logger.debug("epic_orchestrator: config file not found at %s; using defaults", config_path)
        cfg = dict(defaults)
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("epic_orchestrator: failed to read config (%s); using defaults", exc)
        cfg = dict(defaults)

    return cfg
