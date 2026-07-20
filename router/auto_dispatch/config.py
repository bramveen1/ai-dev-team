"""Constants and config loading for the auto-dispatch loop.

Every tunable and magic value for the loop lives here so the behaviour
modules (``loop``, ``github``, ``triage``, …) stay free of configuration
noise. ``load_auto_dispatch_config`` is re-read on every tick so YAML
changes take effect without a restart.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from router import github_api

logger = logging.getLogger(__name__)

CALLABLE_REF = "router.auto_dispatch:tick"
TASK_NAME = "auto-dispatch-bug-loop"

# Default cadence for the scheduler system task (every 30 min = 2/hr ceiling).
DEFAULT_PERIOD_SECONDS = 1800

# Where we persist the daily/hourly counter so restarts don't reset the cap.
DEFAULT_COUNTER_PATH = "/var/lib/dispatch/_auto_dispatch_counters.json"

# How long an awaiting entry may sit with no PR before we give up on it (the
# worker most likely failed to open one). Stops the awaiting set growing forever.
AWAITING_MAX_AGE_SECONDS = 24 * 3600

# How long a pending-approval entry blocks re-dispatch. Prevents the same
# approval card being re-posted on every tick during the human-decision window.
# 7 days covers a typical review cycle; after expiry the issue becomes eligible
# again (Deny + TTL expiry is the natural cooldown path).
PENDING_APPROVAL_MAX_AGE_SECONDS = 7 * 24 * 3600

# GitHub token for this loop's API calls: listing issues/PRs, reading CI, and
# applying the ``auto-merge`` label. This loop does NOT merge — merging is
# delegated to merge_queue.py (the ``aidt-merge`` identity). The token reused
# here happens to be aidt-merge's; no new secret is introduced.
MERGE_PAT_PATH = github_api.MERGE_PAT_PATH

# Label that signals the merge queue may squash-merge a CI-green PR.
AUTO_MERGE_LABEL = "auto-merge"

# Required CI check names that must all pass before we hand a PR to the queue.
REQUIRED_CHECKS: frozenset[str] = frozenset({"lint", "test-unit", "test-integration", "docker-build", "compose-check"})

# AC section marker (case-sensitive, as specified in issue).
AC_SECTION_RE = re.compile(r"^##\s+Acceptance Criteria", re.MULTILINE)

# Head-branch prefix used by dev-worker dispatches (e.g. ``issue-42-fix-bug``).
DEV_WORKER_BRANCH_PREFIX = "issue-"


def load_auto_dispatch_config(config_path: str | None = None) -> dict:
    """Load ``auto_dispatch`` block from ``config/dispatch.yaml``.

    Returns a dict with all keys present (defaults filled in). The caller
    should not cache this — re-read on every tick so config changes take
    effect without a restart.
    """
    defaults: dict[str, Any] = {
        "enabled": False,
        "rate_per_hour": 2,
        "daily_cap": 6,
        "shadow_mode": True,
    }

    if config_path is None:
        # config/dispatch.yaml relative to the repo root (three levels up from
        # this file: auto_dispatch/ → router/ → repo root).
        config_path = str(Path(__file__).resolve().parents[2] / "config" / "dispatch.yaml")

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        block = raw.get("auto_dispatch") or {}
        cfg = {**defaults, **block}
    except FileNotFoundError:
        logger.debug("auto_dispatch: config file not found at %s; using defaults", config_path)
        cfg = dict(defaults)
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("auto_dispatch: failed to read config (%s); using defaults", exc)
        cfg = dict(defaults)

    return cfg
