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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CALLABLE_REF = "router.auto_dispatch:tick"
TASK_NAME = "auto-dispatch-bug-loop"

# Default cadence for the scheduler system task (every 30 min = 2/hr ceiling).
DEFAULT_PERIOD_SECONDS = 1800

# Where we persist the daily/hourly counter so restarts don't reset the cap.
DEFAULT_COUNTER_PATH = "/var/lib/dispatch/_auto_dispatch_counters.json"

# How long an awaiting entry may sit with no PR before we give up on it (the
# worker most likely failed to open one). Stops the awaiting set growing forever.
AWAITING_MAX_AGE_SECONDS = 24 * 3600

# Triage deny-list: path glob → reason label.  Evaluated in order; first
# match wins.  Any path that matches any entry routes to "hold".
TRIAGE_DENY_GLOBS: tuple[tuple[str, str], ...] = (
    # Auth
    ("**/auth/**", "auth"),
    ("**/authentication/**", "auth"),
    ("**/*auth*", "auth"),
    # Money / billing
    ("**/billing/**", "billing"),
    ("**/payment/**", "billing"),
    ("**/invoice/**", "billing"),
    ("**/*billing*", "billing"),
    ("**/*payment*", "billing"),
    # DB migrations
    ("**/migrations/**", "db_migration"),
    ("**/alembic/**", "db_migration"),
    ("**/*.sql", "db_migration"),
    ("**/*migration*", "db_migration"),
    ("**/*migrate*", "db_migration"),
    # Deploy / compose config
    ("**/docker-compose*", "deploy_config"),
    ("**/Dockerfile*", "deploy_config"),
    ("**/systemd/**", "deploy_config"),
    ("**/.github/**", "deploy_config"),
    ("**/deploy/**", "deploy_config"),
    # Secrets
    ("**/secrets/**", "secrets"),
    ("**/.env*", "secrets"),
    ("**/*secret*", "secrets"),
    ("**/*token*", "secrets"),
)

GITHUB_API_BASE = "https://api.github.com"
# GitHub token for this loop's API calls: listing issues/PRs, reading CI, and
# applying the ``auto-merge`` label. This loop does NOT merge — merging is
# delegated to merge_queue.py (the ``aidt-merge`` identity). The token reused
# here happens to be aidt-merge's; no new secret is introduced.
MERGE_PAT_PATH = "/config/secrets/gh-aidt-merge.token"

# Label that signals the merge queue may squash-merge a CI-green PR.
AUTO_MERGE_LABEL = "auto-merge"

# Required CI check names that must all pass before we hand a PR to the queue.
REQUIRED_CHECKS: frozenset[str] = frozenset({"lint", "test-unit", "test-integration", "docker-build", "compose-check"})

# AC section marker (case-sensitive, as specified in issue).
AC_SECTION_RE = re.compile(r"^##\s+Acceptance Criteria", re.MULTILINE)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


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
        "multi_file_threshold": 1,
    }

    if config_path is None:
        # config/dispatch.yaml relative to the repo root (two levels up from this file).
        config_path = str(Path(__file__).resolve().parent.parent / "config" / "dispatch.yaml")

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        block = raw.get("auto_dispatch") or {}
        return {**defaults, **block}
    except FileNotFoundError:
        logger.debug("auto_dispatch: config file not found at %s; using defaults", config_path)
        return defaults
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("auto_dispatch: failed to read config (%s); using defaults", exc)
        return defaults


# ---------------------------------------------------------------------------
# Persisted counters (daily cap + hourly rate, survive restarts)
# ---------------------------------------------------------------------------


def _read_counters(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_counters(path: str, data: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)
    except OSError as exc:
        logger.warning("auto_dispatch: failed to write counters to %s: %s", path, exc)


def _today_str(now: float | None = None) -> str:
    """Return YYYY-MM-DD in UTC for the given timestamp (or now)."""
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _current_hour_str(now: float | None = None) -> str:
    """Return YYYY-MM-DDTHH in UTC for the given timestamp (or now)."""
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H")


def get_counters(counter_path: str, now: float | None = None) -> dict:
    """Return ``{daily_count, hourly_count}`` for the current window."""
    data = _read_counters(counter_path)
    today = _today_str(now)
    hour = _current_hour_str(now)
    return {
        "daily_count": data.get("daily_count", 0) if data.get("daily_date") == today else 0,
        "hourly_count": data.get("hourly_count", 0) if data.get("hourly_hour") == hour else 0,
    }


def increment_counters(counter_path: str, now: float | None = None) -> None:
    """Atomically bump daily and hourly dispatch counters."""
    data = _read_counters(counter_path)
    today = _today_str(now)
    hour = _current_hour_str(now)

    daily_count = data.get("daily_count", 0) if data.get("daily_date") == today else 0
    hourly_count = data.get("hourly_count", 0) if data.get("hourly_hour") == hour else 0

    _write_counters(
        counter_path,
        {
            "daily_date": today,
            "daily_count": daily_count + 1,
            "hourly_hour": hour,
            "hourly_count": hourly_count + 1,
        },
    )


# ---------------------------------------------------------------------------
# Awaiting tracker — issues we dispatched that still need verdict + labelling.
#
# Keyed by issue number → enqueue timestamp. Persisted (atomic, beside the
# counter file) so a restart doesn't lose track of in-flight auto-dispatches.
# This is the bridge that makes ``handle_pr_verdict`` actually get *called*:
# ``tick`` drains this set every cycle, independent of the new-dispatch caps.
# ---------------------------------------------------------------------------


def _awaiting_path(payload: dict) -> str:
    explicit = payload.get("awaiting_path")
    if explicit:
        return explicit
    counter_path = payload.get("counter_path", DEFAULT_COUNTER_PATH)
    return str(Path(counter_path).with_name("_auto_dispatch_awaiting.json"))


def _read_awaiting(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_awaiting(path: str, data: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)
    except OSError as exc:
        logger.warning("auto_dispatch: failed to write awaiting set to %s: %s", path, exc)


def _add_awaiting(path: str, issue_num: int, now_ts: float) -> None:
    data = _read_awaiting(path)
    data[str(issue_num)] = now_ts
    _write_awaiting(path, data)


def _remove_awaiting(path: str, issue_num: int) -> None:
    data = _read_awaiting(path)
    if data.pop(str(issue_num), None) is not None:
        _write_awaiting(path, data)


# ---------------------------------------------------------------------------
# Triage gate (machine-checkable — no LLM vibes)
# ---------------------------------------------------------------------------


def _compile_glob(pattern: str) -> re.Pattern:
    """Compile a ``**``-glob pattern to a regex.

    Semantics:
    - ``**`` matches zero or more path components (including ``/``).
    - ``*`` matches any characters except ``/``.
    - ``?`` matches any single character except ``/``.

    Works on Python 3.10+ (``Path.match`` gained full ``**`` support only
    in 3.12; this helper uses regex so it is version-independent).
    """
    # Split on "**" to isolate the double-star segments.
    parts = pattern.split("**")
    re_parts: list[str] = []
    for segment in parts:
        # Within each non-** segment, convert * → [^/]* and ? → [^/], and
        # escape all other regex-special characters.
        seg_re = ""
        for ch in segment:
            if ch == "*":
                seg_re += "[^/]*"
            elif ch == "?":
                seg_re += "[^/]"
            elif ch in r"\.+^${}()|[]":
                seg_re += "\\" + ch
            else:
                seg_re += ch
        re_parts.append(seg_re)

    if len(re_parts) == 1:
        # No ** in pattern — match the full path (or just filename).
        return re.compile(f"^(?:.*/)?{re_parts[0]}$", re.IGNORECASE)

    # Join segments with .*  (** = any characters including /).
    # Then post-process: replace `.*<sep>` and `<sep>.*` edge patterns so
    # that **/ at the start means "zero or more leading directories" and
    # /** at the end means "zero or more trailing path components".
    combined = ".*".join(re_parts)
    # Replace `.*/<something>` with `(?:.*/<something>|<something>)` so the
    # leading **/ is truly optional (matches files at the repo root too).
    # We do this by replacing a leading `.*` followed by `/` with `(?:.*/)?`.
    combined = re.sub(r"^\.\*/", "(?:.*/)?", combined)
    # Replace a trailing `/<something>.*` → `(?:/.*)?` suffix for /** at end.
    combined = re.sub(r"/\.\*$", "(?:/.*)?", combined)
    return re.compile(f"^{combined}$", re.IGNORECASE)


def _path_matches_deny(file_path: str, deny_globs: tuple[tuple[str, str], ...] = TRIAGE_DENY_GLOBS) -> str | None:
    """Return the reason label if ``file_path`` matches any deny-list glob, else None.

    Uses a regex compiled from each glob pattern so ``**`` semantics work on
    Python 3.10+ (``Path.match`` gained full ``**`` support only in 3.12).
    The path is normalised to POSIX forward-slash form before matching.

    Bias to hold: an ambiguous/unrecognisable path falls through all patterns
    and returns None from this function, but :func:`triage` catches the empty
    case before calling here.
    """
    normalised = Path(file_path).as_posix()
    for glob, reason in deny_globs:
        pattern_re = _compile_glob(glob)
        if pattern_re.match(normalised):
            return reason
    return None


def triage(
    changed_files: list[str],
    *,
    multi_file_threshold: int = 1,
    deny_globs: tuple[tuple[str, str], ...] = TRIAGE_DENY_GLOBS,
) -> tuple[str, str]:
    """Evaluate blast radius and return ``(decision, reason)``.

    ``decision`` is one of:

    * ``"low_risk"`` — safe to auto-merge after verdict+CI gate.
    * ``"hold"``     — hold for human review.

    Bias to hold — false-negative (calling a sensitive change low_risk) is
    the dangerous direction:

    1. **No files** — ``hold`` (unknown diff).
    2. **Multi-file** — ``hold`` if ``len(files) > multi_file_threshold``.
    3. **Deny-list match** — ``hold`` on first match.
    4. **Remaining single file** — ``low_risk``.
    """
    if not changed_files:
        return "hold", "unknown_diff"

    if len(changed_files) > multi_file_threshold:
        return "hold", f"multi_file:{len(changed_files)}"

    for file_path in changed_files:
        reason = _path_matches_deny(file_path, deny_globs)
        if reason is not None:
            return "hold", reason

    return "low_risk", "clean"


# ---------------------------------------------------------------------------
# GitHub API helpers (mirrors patterns from merge_queue.py)
# ---------------------------------------------------------------------------


def _read_pat(path: str = MERGE_PAT_PATH) -> str:
    try:
        token = Path(path).read_text().strip()
    except FileNotFoundError:
        raise _TokenError(f"PAT file not found: {path}") from None
    except OSError as exc:
        raise _TokenError(f"Cannot read PAT file {path}: {exc}") from exc
    if not token:
        raise _TokenError(f"PAT file is empty: {path}")
    return token


class _TokenError(Exception):
    pass


def _auth_headers(pat: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _gh_get(path: str, pat: str, **params: Any) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.get(url, headers=_auth_headers(pat), params=params)


async def _gh_put(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.put(url, headers=_auth_headers(pat), json=body or {})


async def _gh_post(path: str, pat: str, body: dict | None = None) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(url, headers=_auth_headers(pat), json=body or {})


async def _get_open_bug_issues(repo: str, pat: str) -> list[dict]:
    """Return open issues labeled 'bug', sorted ascending by number."""
    resp = await _gh_get(
        f"/repos/{repo}/issues",
        pat,
        state="open",
        labels="bug",
        sort="created",
        direction="asc",
        per_page=100,
    )
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 listing issues for {repo}")
    resp.raise_for_status()
    issues: list[dict] = resp.json()
    # Filter out pull requests (GitHub issues endpoint returns PRs too).
    return [i for i in issues if "pull_request" not in i]


async def _get_open_dev_prs(repo: str, pat: str) -> list[dict]:
    """Return open PRs whose head branch starts with a dev-worker prefix."""
    resp = await _gh_get(
        f"/repos/{repo}/pulls",
        pat,
        state="open",
        per_page=100,
    )
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 listing PRs for {repo}")
    resp.raise_for_status()
    return resp.json()


async def _get_pr_files(repo: str, pr_num: int, pat: str) -> list[str]:
    """Return the list of file paths changed in a PR."""
    resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}/files", pat, per_page=100)
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 fetching files for PR #{pr_num}")
    resp.raise_for_status()
    return [f["filename"] for f in resp.json()]


async def _get_pr_for_issue(repo: str, issue_num: int, pat: str) -> dict | None:
    """Return the first open PR that references this issue number, or None."""
    prs = await _get_open_dev_prs(repo, pat)
    for pr in prs:
        body = pr.get("body") or ""
        title = pr.get("title") or ""
        # Match common "Fixes #NNN" / "Closes #NNN" / "Resolves #NNN" patterns.
        if re.search(rf"\b(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#{issue_num}\b", body + " " + title, re.IGNORECASE):
            return pr
    return None


async def _get_pr_details(repo: str, pr_num: int, pat: str) -> dict:
    resp = await _gh_get(f"/repos/{repo}/pulls/{pr_num}", pat)
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 fetching PR #{pr_num}")
    resp.raise_for_status()
    return resp.json()


async def _get_check_runs(repo: str, head_sha: str, pat: str) -> dict[str, dict]:
    """Return latest check run per name (keyed by name)."""
    all_runs: list[dict] = []
    page = 1
    while True:
        resp = await _gh_get(f"/repos/{repo}/commits/{head_sha}/check-runs", pat, per_page=100, page=page)
        if resp.status_code == 401:
            raise _TokenError(f"GitHub 401 fetching check-runs for {head_sha}")
        resp.raise_for_status()
        batch: list[dict] = resp.json().get("check_runs", [])
        all_runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    latest: dict[str, dict] = {}
    for run in all_runs:
        name = run["name"]
        if name not in latest or run.get("id", 0) > latest[name].get("id", 0):
            latest[name] = run
    return latest


async def _ci_green(repo: str, head_sha: str, pat: str) -> bool:
    """True when all required CI checks passed for ``head_sha``."""
    latest = await _get_check_runs(repo, head_sha, pat)
    passed = {name for name, run in latest.items() if run.get("conclusion") == "success"}
    return REQUIRED_CHECKS.issubset(passed)


async def _apply_auto_merge_label(repo: str, pr_num: int, pat: str) -> bool:
    """Apply the ``auto-merge`` label so merge_queue.py picks the PR up.

    This is the loop's terminal action on the safe path — it hands the PR to the
    single merger (the queue) rather than merging here. Idempotent: GitHub's
    add-labels endpoint is a no-op if the label is already present.
    """
    resp = await _gh_post(
        f"/repos/{repo}/issues/{pr_num}/labels",
        pat,
        {"labels": [AUTO_MERGE_LABEL]},
    )
    if resp.status_code in (200, 201):
        return True
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 labelling PR #{pr_num}")
    logger.warning(
        "auto_dispatch: applying %s label returned %s for PR #%s",
        AUTO_MERGE_LABEL,
        resp.status_code,
        pr_num,
    )
    return False


# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------


async def _slack_post(slack_client: Any, channel: str | None, text: str) -> None:
    if slack_client is None or not channel:
        logger.info("auto_dispatch: no Slack client/channel; skipping post: %s", text)
        return
    try:
        await slack_client.chat_postMessage(channel=channel, text=text)
    except Exception:
        logger.exception("auto_dispatch: failed to post notification")


async def _slack_post_with_ts(slack_client: Any, channel: str | None, text: str) -> str:
    """Post a message and return its ``ts`` (empty string if posting fails or no channel)."""
    if slack_client is None or not channel:
        logger.info("auto_dispatch: no Slack client/channel; skipping post: %s", text)
        return ""
    try:
        resp = await slack_client.chat_postMessage(channel=channel, text=text)
        return (resp or {}).get("ts") or ""
    except Exception:
        logger.exception("auto_dispatch: failed to post notification")
        return ""


# ---------------------------------------------------------------------------
# Eligibility selector
# ---------------------------------------------------------------------------


def _has_ac_block(issue_body: str) -> bool:
    """True when the issue body contains an ``## Acceptance Criteria`` section."""
    return bool(AC_SECTION_RE.search(issue_body or ""))


async def pick_next_candidate(
    repo: str,
    pat: str,
    *,
    in_flight_issue_nums: set[int] | None = None,
) -> dict | None:
    """Return the next eligible bug issue, or None.

    Eligible: open, labeled bug, has ``## Acceptance Criteria`` block,
    not already dispatched/in-flight.  Skipped issues are logged with reason.
    """
    issues = await _get_open_bug_issues(repo, pat)
    in_flight = in_flight_issue_nums or set()

    for issue in issues:
        issue_num = issue["number"]
        body = issue.get("body") or ""

        if issue_num in in_flight:
            logger.debug("auto_dispatch: skip issue #%s — already in flight", issue_num)
            continue

        if not _has_ac_block(body):
            logger.info("auto_dispatch: skip issue #%s — no AC block", issue_num)
            continue

        logger.info("auto_dispatch: candidate issue #%s — %s", issue_num, issue.get("title", ""))
        return issue

    return None


# ---------------------------------------------------------------------------
# Loop conditions
# ---------------------------------------------------------------------------


def _merge_queue_empty(open_prs: list[dict]) -> bool:
    """True when there are no open PRs (proxy for an empty merge queue).

    The real merge queue lives in merge_queue.py; for the auto-dispatch
    eligibility check we treat «zero open PRs» as equivalent to an empty
    queue (conservative — blocks dispatch when any PR is open).
    """
    return len(open_prs) == 0


def _count_open_dev_prs(open_prs: list[dict]) -> int:
    return len(open_prs)


# ---------------------------------------------------------------------------
# In-flight tracker (one bug in flight at a time)
# ---------------------------------------------------------------------------


def _get_in_flight_issue_nums(dispatch_root_override: str | None = None) -> set[int]:
    """Return issue numbers of all currently in-flight dispatches.

    An in-flight dispatch has no ``exitcode`` file yet. We read the
    ``issue_url`` sidecar and parse the issue number from it.
    """
    from router.dispatch import state as dstate

    result: set[int] = set()
    for dispatch_id in dstate.list_dispatch_ids(root=dispatch_root_override):
        if dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root_override) is not None:
            continue
        issue_url = dstate.read_field(dispatch_id, dstate.FIELD_ISSUE_URL, root=dispatch_root_override) or ""
        m = re.search(r"/issues/(\d+)$", issue_url)
        if m:
            result.add(int(m.group(1)))
    return result


def _has_any_in_flight_dispatch(dispatch_root_override: str | None = None) -> bool:
    """True when at least one dispatch has no exitcode (still running)."""
    from router.dispatch import state as dstate

    for dispatch_id in dstate.list_dispatch_ids(root=dispatch_root_override):
        if dstate.read_field(dispatch_id, dstate.FIELD_EXITCODE, root=dispatch_root_override) is None:
            return True
    return False


# ---------------------------------------------------------------------------
# Verdict helper
# ---------------------------------------------------------------------------


async def _get_verdict_from_pr(repo: str, pr_num: int, pat: str) -> str | None:
    """Return ``'pass'``, ``'fail'``, or None (no verdict posted yet).

    We look for a structured verdict comment posted by the review identity
    (``bramveen1``).  The comment must contain a line matching
    ``verdict: pass`` or ``verdict: fail`` (case-insensitive).
    """
    resp = await _gh_get(f"/repos/{repo}/issues/{pr_num}/comments", pat, per_page=100)
    if resp.status_code == 401:
        raise _TokenError(f"GitHub 401 fetching comments for PR #{pr_num}")
    resp.raise_for_status()
    verdict_re = re.compile(r"^verdict:\s*(pass|fail)", re.IGNORECASE | re.MULTILINE)
    for comment in reversed(resp.json()):
        user_login = (comment.get("user") or {}).get("login", "")
        if user_login not in ("bramveen1", "aidt-merge"):
            continue
        m = verdict_re.search(comment.get("body") or "")
        if m:
            return m.group(1).lower()
    return None


# ---------------------------------------------------------------------------
# Awaiting driver — drives dispatched issues through verdict + labelling
# ---------------------------------------------------------------------------


async def _process_awaiting(
    *,
    repo: str,
    pat: str,
    slack_client: Any,
    destination: str | None,
    cfg: dict,
    payload: dict,
    now_ts: float,
) -> None:
    """Drive every awaited (already-dispatched) issue through ``handle_pr_verdict``.

    Runs every tick, independent of the new-dispatch rate caps — finishing
    in-flight work is not rate-limited. For each awaited issue:

    * find its open PR (``_get_pr_for_issue``);
    * ``None`` → either already merged/closed (terminal) or the worker hasn't
      opened a PR yet (keep until ``AWAITING_MAX_AGE_SECONDS``, then expire);
    * otherwise call ``handle_pr_verdict``: ``pending`` (no verdict yet) keeps
      the entry for the next tick; any other status is terminal for the tracker
      (labeled / would_label = handed to the merge queue; hold / fail / error =
      a human owns it now).
    """
    awaiting_path = _awaiting_path(payload)
    awaiting = _read_awaiting(awaiting_path)
    if not awaiting:
        return

    pat_path = payload.get("pat_path", MERGE_PAT_PATH)
    counter_path = payload.get("counter_path", DEFAULT_COUNTER_PATH)

    for issue_str, enqueued_ts in list(awaiting.items()):
        try:
            issue_num = int(issue_str)
        except (TypeError, ValueError):
            # Corrupt key — drop it so it can't wedge the loop.
            data = _read_awaiting(awaiting_path)
            data.pop(issue_str, None)
            _write_awaiting(awaiting_path, data)
            continue

        pr = await _get_pr_for_issue(repo, issue_num, pat)
        if pr is None:
            try:
                age = now_ts - float(enqueued_ts)
            except (TypeError, ValueError):
                age = AWAITING_MAX_AGE_SECONDS + 1
            if age > AWAITING_MAX_AGE_SECONDS:
                logger.warning(
                    "auto_dispatch: issue #%s awaited > %ss with no open PR; dropping from tracker",
                    issue_num,
                    AWAITING_MAX_AGE_SECONDS,
                )
                _remove_awaiting(awaiting_path, issue_num)
            else:
                logger.info("auto_dispatch: issue #%s has no open PR yet; will recheck next tick", issue_num)
            continue

        pr_num = pr["number"]
        outcome = await handle_pr_verdict(
            repo=repo,
            pr_num=pr_num,
            issue_num=issue_num,
            slack_client=slack_client,
            destination=destination,
            pat_path=pat_path,
            shadow_mode=cfg["shadow_mode"],
            multi_file_threshold=cfg["multi_file_threshold"],
            counter_path=counter_path,
            now=now_ts,
        )
        status = outcome.get("status")
        if status == "pending":
            # Verdict not posted yet — keep awaiting, recheck next tick.
            continue
        # Everything else is terminal for the tracker. (CI-not-green is treated
        # as terminal/hold-for-human by design: by the time a verdict exists CI
        # is normally complete, and biasing to a human on red is the safe side.)
        _remove_awaiting(awaiting_path, issue_num)
        logger.info(
            "auto_dispatch: issue #%s PR #%s reached terminal status=%s reason=%s",
            issue_num,
            pr_num,
            status,
            outcome.get("reason"),
        )


# ---------------------------------------------------------------------------
# Main tick callable (invoked by the scheduler system task)
# ---------------------------------------------------------------------------


async def tick(*, payload: dict, slack_client: Any, now: datetime) -> dict:
    """Self-bounded entry point invoked by the scheduler system task.

    #502: the scheduler awaits system tasks INLINE with no timeout, so a slow
    tick (serial GitHub IO) would freeze ``run_once`` and every other due task.
    The general fix belongs in the scheduler and is tracked in #502; until then
    we defend the loop here by capping the whole tick at ``tick_timeout`` seconds.
    Always returns ``{"status": "ok", ...}`` — the task is permanent.
    """
    timeout = payload.get("tick_timeout", 120)
    try:
        return await asyncio.wait_for(
            _tick_impl(payload=payload, slack_client=slack_client, now=now),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("auto_dispatch: tick exceeded %ss budget; aborting this cycle to protect the scheduler", timeout)
        return {"status": "ok", "skipped": "tick_timeout"}


async def _tick_impl(*, payload: dict, slack_client: Any, now: datetime) -> dict:
    """Auto-dispatch system-task callable, invoked by the scheduler every period.

    Always returns ``{"status": "ok"}`` — the task is permanent (never deregisters).

    Decision tree per tick:

    1. Read config; bail if disabled.
    2. Check rate/daily caps.
    3. Assert no in-flight dispatch (one-in-flight gate).
    4. Fetch open PRs; assert merge queue empty and zero open dev PRs.
    5. Pick next eligible candidate issue.
    6. Triage (path-glob + file-count); hold if sensitive.
    7. Dispatch worker against the issue's AC block.
    8. (After worker PR lands) get verdict + CI.
    9. Shadow mode → "would label"; live mode → apply ``auto-merge`` label
       (merge_queue.py performs the actual merge on a later tick).
    """
    repo: str = payload.get("repo", "")
    pat_path: str = payload.get("pat_path", MERGE_PAT_PATH)
    destination: str | None = payload.get("destination") or os.environ.get("BRAM_DM_CHANNEL")
    counter_path: str = payload.get("counter_path", DEFAULT_COUNTER_PATH)
    config_path: str | None = payload.get("config_path")

    if not repo:
        logger.error("auto_dispatch: payload missing 'repo'; skipping tick")
        return {"status": "ok", "skipped": "no_repo"}

    # 1. Config — read fresh every tick so changes take effect without restart.
    cfg = load_auto_dispatch_config(config_path)

    if not cfg["enabled"]:
        logger.debug("auto_dispatch: disabled (auto_dispatch.enabled=false)")
        return {"status": "ok", "skipped": "disabled"}

    now_ts = now.timestamp()

    # 1b. Drive already-dispatched issues through verdict + labelling BEFORE the
    # new-dispatch caps — finishing in-flight work is not rate-limited. Reading
    # the PAT here is best-effort: if it's unavailable we skip awaiting
    # processing and fall through to the (capped) new-dispatch path below.
    try:
        _awaiting_pat = _read_pat(pat_path)
    except _TokenError:
        _awaiting_pat = None
    if _awaiting_pat is not None:
        try:
            await _process_awaiting(
                repo=repo,
                pat=_awaiting_pat,
                slack_client=slack_client,
                destination=destination,
                cfg=cfg,
                payload=payload,
                now_ts=now_ts,
            )
        except (_TokenError, httpx.HTTPError) as exc:
            logger.error("auto_dispatch: awaiting processing error: %s", exc)

    counters = get_counters(counter_path, now_ts)

    # 2a. Daily cap.
    if counters["daily_count"] >= cfg["daily_cap"]:
        logger.info("auto_dispatch: daily cap reached (%d/%d)", counters["daily_count"], cfg["daily_cap"])
        return {"status": "ok", "skipped": "daily_cap"}

    # 2b. Hourly rate.
    if counters["hourly_count"] >= cfg["rate_per_hour"]:
        logger.info("auto_dispatch: hourly rate reached (%d/%d)", counters["hourly_count"], cfg["rate_per_hour"])
        return {"status": "ok", "skipped": "hourly_rate"}

    # 3. One-in-flight gate.
    if _has_any_in_flight_dispatch():
        logger.info("auto_dispatch: dispatch already in flight; suppressing")
        return {"status": "ok", "skipped": "in_flight"}

    # Read PAT — fail loud.
    try:
        pat = _read_pat(pat_path)
    except _TokenError as exc:
        msg = f":x: auto-dispatch: {exc}"
        logger.error("auto_dispatch: %s", exc)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "skipped": "token_error"}

    # 4. Fetch open PRs; check merge queue empty and zero open dev PRs.
    try:
        open_prs = await _get_open_dev_prs(repo, pat)
    except _TokenError as exc:
        logger.error("auto_dispatch: %s", exc)
        await _slack_post(slack_client, destination, f":x: auto-dispatch: {exc}")
        return {"status": "ok", "skipped": "token_error"}
    except httpx.HTTPError as exc:
        logger.error("auto_dispatch: HTTP error listing PRs: %s", exc)
        return {"status": "ok", "skipped": "http_error"}

    if open_prs:
        logger.info("auto_dispatch: %d open PR(s) — suppressing dispatch", len(open_prs))
        return {"status": "ok", "skipped": f"open_prs:{len(open_prs)}"}

    # 5. Pick next eligible bug. Exclude both live dispatches (no exitcode yet)
    # AND anything still in the awaiting tracker (PR open, verdict pending) so
    # we never re-dispatch an issue whose PR hasn't been detected as open yet.
    in_flight_nums = _get_in_flight_issue_nums()
    in_flight_nums |= {int(k) for k in _read_awaiting(_awaiting_path(payload)) if str(k).isdigit()}
    try:
        candidate = await pick_next_candidate(repo, pat, in_flight_issue_nums=in_flight_nums)
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch: error picking candidate: %s", exc)
        return {"status": "ok", "skipped": "candidate_error"}

    if candidate is None:
        logger.info("auto_dispatch: no eligible bug issues found")
        return {"status": "ok", "skipped": "no_candidate"}

    issue_num = candidate["number"]
    issue_title = candidate.get("title", f"issue #{issue_num}")
    issue_url = candidate.get("html_url", f"https://github.com/{repo}/issues/{issue_num}")

    # 6. Triage: we don't have a PR diff yet (no PR exists), so we triage
    # based on the issue's title/labels as a best-effort pre-dispatch gate.
    # If the issue body hints at sensitive areas we hold early; otherwise
    # we let the post-dispatch triage (on the actual PR files) make the call.
    # The actual diff-based triage runs after the worker PR lands (step 8).
    # Pre-dispatch: conservatively check issue title/body for deny keywords.
    triage_decision, triage_reason = _pre_dispatch_triage(candidate, multi_file_threshold=cfg["multi_file_threshold"])

    if triage_decision == "hold":
        msg = (
            f":warning: auto-dispatch: issue #{issue_num} pre-dispatch triage → *hold* "
            f"(reason: {triage_reason}) — {issue_url}"
        )
        logger.info("auto_dispatch: issue #%s triage=hold reason=%s", issue_num, triage_reason)
        await _slack_post(slack_client, destination, msg)
        return {"status": "ok", "action": "hold", "issue": issue_num, "reason": triage_reason}

    # 7. Shadow mode gate — never spawn a real worker in shadow mode.
    if cfg["shadow_mode"]:
        msg = (
            f":ghost: auto-dispatch (shadow): would dispatch worker for issue #{issue_num} "
            f"({issue_title}) — {issue_url}"
        )
        await _slack_post(slack_client, destination, msg)
        logger.info(
            "auto_dispatch: shadow mode — would dispatch worker for issue #%s",
            issue_num,
        )
        return {"status": "ok", "action": "would_dispatch", "issue": issue_num}

    # 7b. Post a kickoff message to anchor the autonomous dispatch to a Slack thread.
    # The autonomous path has no originating thread; we create one here so the
    # dispatch handler receives a valid thread_ts instead of an empty string.
    kickoff_ts = await _slack_post_with_ts(
        slack_client,
        destination,
        f":gear: auto-dispatch: starting worker for issue #{issue_num} ({issue_title}) — {issue_url}",
    )

    # 7b-guard: no Slack channel / client → kickoff_ts is empty.  Proceeding
    # would cause the handler to fail with missing_slack_context.  Bail out
    # deterministically (#563 negative-path requirement).
    if not kickoff_ts:
        logger.error(
            "auto_dispatch: empty kickoff_ts for issue #%s (no Slack channel or post failed); "
            "skipping dispatch to avoid missing_slack_context on the handler leg",
            issue_num,
        )
        return {"status": "ok", "skipped": "empty_slack_context", "issue": issue_num}

    # 7c. Dispatch worker.
    logger.info("auto_dispatch: dispatching worker for issue #%s (%s)", issue_num, issue_title)
    worker_outcome: str = ""
    try:
        worker_outcome = await asyncio.wait_for(
            _dispatch_worker(
                issue_url=issue_url,
                issue_num=issue_num,
                issue_title=issue_title,
                slack_client=slack_client,
                destination=destination,
                thread_ts=kickoff_ts,
                payload=payload,
            ),
            timeout=payload.get("dispatch_timeout", 60),
        )
    except asyncio.TimeoutError:
        logger.warning("auto_dispatch: worker dispatch timed out for issue #%s", issue_num)
        return {"status": "ok", "skipped": "dispatch_timeout"}
    except Exception:
        logger.exception("auto_dispatch: worker dispatch error for issue #%s", issue_num)
        return {"status": "ok", "skipped": "dispatch_error"}

    # approval_required: gate fired, draft posted, human must click Approve.
    # Do NOT increment counters or enrol in awaiting — no worker was spawned.
    if worker_outcome == "approval_required":
        return {"status": "ok", "action": "approval_required", "issue": issue_num}

    # Increment counters only after a successful dispatch kick-off.
    increment_counters(counter_path, now_ts)

    # Enrol the issue in the awaiting tracker so a *later* tick drives it
    # through verdict + labelling via ``_process_awaiting``. Without this the
    # whole verdict bridge never fires — the dispatch would land a PR that no
    # tick ever picks up. Persisted (atomic) so a restart doesn't lose it.
    _add_awaiting(_awaiting_path(payload), issue_num, now_ts)

    msg = f":rocket: auto-dispatch: dispatched worker for issue #{issue_num} ({issue_title}) — {issue_url}"
    await _slack_post(slack_client, destination, msg)

    # 8. The verdict + labelling step is driven on a *subsequent* tick by
    # ``_process_awaiting`` (step 1b above): once the worker opens a PR and a
    # verdict lands, ``handle_pr_verdict`` runs the diff-based triage + gated
    # ``auto-merge`` labelling, then removes the issue from the awaiting set
    # (the merge queue owns it from there). This tick is done.
    return {"status": "ok", "action": "dispatched", "issue": issue_num}


# ---------------------------------------------------------------------------
# Pre-dispatch triage (issue-level, before a PR exists)
# ---------------------------------------------------------------------------

# Keywords that map directly to deny-list reasons when found in issue title/body.
_PRESCAN_DENY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bauth(?:entication|orization)?\b", re.IGNORECASE), "auth"),
    (re.compile(r"\bbilling\b|\bpayment\b|\binvoice\b", re.IGNORECASE), "billing"),
    (re.compile(r"\bmigration\b|\balembic\b", re.IGNORECASE), "db_migration"),
    (re.compile(r"\bdocker-compose\b|\bdockerfile\b|\bsystemd\b|\bdeploy\b", re.IGNORECASE), "deploy_config"),
    (re.compile(r"\bsecret\b|\btoken\b|\bcredential\b", re.IGNORECASE), "secrets"),
)


def _pre_dispatch_triage(issue: dict, *, multi_file_threshold: int = 1) -> tuple[str, str]:
    """Coarse pre-dispatch triage from issue metadata (title + labels).

    Falls back to ``low_risk`` when no deny pattern is matched — the fine-grained
    diff-based triage runs after the worker PR is created.
    """
    text = (issue.get("title") or "") + " " + (issue.get("body") or "")
    for pattern, reason in _PRESCAN_DENY_PATTERNS:
        if pattern.search(text):
            return "hold", reason
    return "low_risk", "pre_dispatch_ok"


# ---------------------------------------------------------------------------
# Verdict + post-dispatch labelling decision
# ---------------------------------------------------------------------------


async def handle_pr_verdict(
    *,
    repo: str,
    pr_num: int,
    issue_num: int,
    slack_client: Any,
    destination: str | None,
    pat_path: str = MERGE_PAT_PATH,
    shadow_mode: bool = True,
    multi_file_threshold: int = 1,
    counter_path: str = DEFAULT_COUNTER_PATH,
    now: float | None = None,
) -> dict:
    """Called (e.g. by supervision) after a worker PR is created.

    Runs the diff-based triage, reads the Sam verdict, checks CI, and on the
    safe path applies the ``auto-merge`` label (live mode) or posts "would
    label" (shadow mode). The actual merge is owned by merge_queue.py.
    """
    try:
        pat = _read_pat(pat_path)
    except _TokenError as exc:
        logger.error("auto_dispatch.handle_pr_verdict: %s", exc)
        return {"status": "error", "reason": "token_error"}

    # Diff-based triage on the real PR files.
    try:
        changed_files = await _get_pr_files(repo, pr_num, pat)
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch.handle_pr_verdict: error fetching PR files: %s", exc)
        return {"status": "error", "reason": "pr_files_error"}

    triage_decision, triage_reason = triage(changed_files, multi_file_threshold=multi_file_threshold)
    logger.info(
        "auto_dispatch.handle_pr_verdict: PR #%s triage=%s reason=%s files=%s",
        pr_num,
        triage_decision,
        triage_reason,
        changed_files,
    )

    pr_url = f"https://github.com/{repo}/pull/{pr_num}"

    if triage_decision == "hold":
        msg = f":warning: auto-dispatch: PR #{pr_num} → *hold for human* (triage reason: {triage_reason}) — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": triage_reason}

    # Get Sam's verdict.
    try:
        verdict = await _get_verdict_from_pr(repo, pr_num, pat)
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch.handle_pr_verdict: error reading verdict: %s", exc)
        return {"status": "error", "reason": "verdict_error"}

    if verdict is None:
        logger.info("auto_dispatch.handle_pr_verdict: no verdict yet for PR #%s", pr_num)
        return {"status": "pending", "reason": "no_verdict"}

    if verdict != "pass":
        msg = f":x: auto-dispatch: PR #{pr_num} verdict=*fail* → hold for human — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": "verdict_fail"}

    # Check CI.
    try:
        pr_details = await _get_pr_details(repo, pr_num, pat)
        head_sha = (pr_details.get("head") or {}).get("sha", "")
        ci_ok = await _ci_green(repo, head_sha, pat) if head_sha else False
    except (_TokenError, httpx.HTTPError) as exc:
        logger.error("auto_dispatch.handle_pr_verdict: CI check error: %s", exc)
        return {"status": "error", "reason": "ci_check_error"}

    if not ci_ok:
        msg = f":x: auto-dispatch: PR #{pr_num} CI not green → hold for human — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": "ci_not_green"}

    # All gates passed → hand the PR to the merge queue (do NOT merge here).
    if shadow_mode:
        msg = (
            f":ghost: auto-dispatch (shadow): would label PR #{pr_num} "
            f"`{AUTO_MERGE_LABEL}` (issue #{issue_num}) — triage=low_risk, verdict=pass, CI=green — {pr_url}"
        )
        await _slack_post(slack_client, destination, msg)
        logger.info(
            "auto_dispatch: shadow mode — would label PR #%s issue #%s %s",
            pr_num,
            issue_num,
            AUTO_MERGE_LABEL,
        )
        return {"status": "would_label", "pr": pr_num, "issue": issue_num}

    # Live mode: apply the auto-merge label; merge_queue.py merges on a later tick.
    try:
        labelled = await _apply_auto_merge_label(repo, pr_num, pat)
    except _TokenError as exc:
        logger.error("auto_dispatch.handle_pr_verdict: label PAT error: %s", exc)
        await _slack_post(slack_client, destination, f":x: auto-dispatch: labelling failed ({exc}) — {pr_url}")
        return {"status": "error", "reason": "label_token_error"}
    except httpx.HTTPError as exc:
        logger.error("auto_dispatch.handle_pr_verdict: HTTP error applying label: %s", exc)
        return {"status": "error", "reason": "label_http_error"}

    if not labelled:
        msg = f":x: auto-dispatch: failed to label PR #{pr_num} `{AUTO_MERGE_LABEL}` — hold for human — {pr_url}"
        await _slack_post(slack_client, destination, msg)
        return {"status": "hold", "reason": "label_failed"}

    msg = (
        f":label: auto-dispatch: labelled PR #{pr_num} `{AUTO_MERGE_LABEL}` "
        f"(issue #{issue_num}) → merge queue will merge when green — {pr_url}"
    )
    await _slack_post(slack_client, destination, msg)
    logger.info("auto_dispatch: labelled PR #%s issue #%s %s → merge queue", pr_num, issue_num, AUTO_MERGE_LABEL)
    return {"status": "labeled", "pr": pr_num, "issue": issue_num}


# ---------------------------------------------------------------------------
# Worker dispatch helper
# ---------------------------------------------------------------------------


async def _default_create_draft_fn(**kwargs: Any) -> None:
    """Default implementation: delegate to ``router.internal_api.create_dispatch_draft``."""
    from router.internal_api import create_dispatch_draft  # noqa: PLC0415

    await create_dispatch_draft(**kwargs)


async def _dispatch_worker(
    *,
    issue_url: str,
    issue_num: int,
    issue_title: str,
    slack_client: Any,
    destination: str | None,
    thread_ts: str = "",
    payload: dict,
    _create_draft_fn: Any = None,
) -> str:
    """Launch a real dev-worker dispatch for *issue_url*.

    Calls the dispatch handler WITHOUT ``--approved`` so the approval gate
    (``approval.require_always`` or the cost/keyword smart gate) is evaluated
    normally.  Two outcomes are handled:

    * ``launched`` — worker spawned successfully; caller enrols in awaiting.
    * ``approval_required`` — the gate fired.  ``_create_draft_fn`` is called
      to persist a draft in the router's store and post the Block Kit approval
      card to Slack.  The human then clicks Approve; ``_execute_approved_draft``
      in ``router.app`` re-invokes the handler with ``--approved``.  No worker
      is spawned here; the caller does NOT enrol in awaiting.

    Any other status raises ``RuntimeError`` so the caller can record the
    failure and avoid marking the issue as awaiting a PR that never started.

    The router process has no ``~/.claude/`` credentials, so calling the handler
    in-process raises ``auth_seed_failed`` (#219) — it MUST run in the agent
    container. This contract is the same one in
    ``router.app._execute_approved_draft``; keep them in sync (#212/#219/#268).

    ``_create_draft_fn`` is injectable for unit tests; the default calls
    ``router.internal_api.create_dispatch_draft`` directly.
    """
    # Import lazily and from the primitive modules (NOT router.app) to avoid a
    # circular import and keep auto_dispatch removable as a single file.
    from router.config import get_agent_map  # noqa: PLC0415
    from router.dispatcher import _run_in_container  # noqa: PLC0415
    from router.packs.dispatch_hook import pack_cli_extras  # noqa: PLC0415

    agent_name = payload.get("worker_agent", "sam")
    agent_map = get_agent_map()
    if agent_name not in agent_map:
        raise RuntimeError(f"auto_dispatch: unknown worker agent {agent_name!r}")
    container = agent_map[agent_name]["container"]

    channel = destination or os.environ.get("BRAM_DM_CHANNEL", "")

    model = payload.get("worker_model", "sonnet") or "sonnet"
    cmd = [
        "python",
        "/config/packs/dispatch/handler.py",
        "dispatch_issue",
        "--issue-url",
        issue_url,
        "--channel",
        channel,
        "--thread-ts",
        thread_ts,
        "--agent",
        agent_name,
        # NOTE: --approved is intentionally omitted so the handler evaluates
        # the approval gate.  The auto path must NOT bypass it (#563).
        "--supervision-mode",
        "poll",
        "--persona",
        payload.get("worker_persona", "dev"),
        "--model",
        model,
    ]
    budget = payload.get("worker_budget_seconds")
    if budget:
        cmd += ["--budget-seconds", str(int(budget))]

    # Inject pack-derived env (notably WORKERS_BOT_TOKEN, #268) so the handler's
    # #257 guard doesn't fire workers_token_missing on the autonomous path.
    extras = pack_cli_extras(agent_name, channel=channel, thread_ts=thread_ts)

    logger.info(
        "auto_dispatch._dispatch_worker: docker-exec dispatch_issue for issue #%s in container=%s agent=%s",
        issue_num,
        container,
        agent_name,
    )
    # Keep the inner docker-exec hard-kill strictly *below* the caller's outer
    # ``wait_for`` budget (``payload["dispatch_timeout"]``, default 60s) so the
    # subprocess is reaped cleanly here rather than being abandoned when the
    # outer timeout cancels this coroutine. Poll mode returns ``launched`` in
    # ~3s, so this ceiling is only a safety backstop.
    exec_timeout = max(10, int(payload.get("dispatch_timeout", 60)) - 5)
    stdout, stderr, _rc = await _run_in_container(
        container=container,
        command=cmd,
        timeout=exec_timeout,
        env=extras.env or None,
    )
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"auto_dispatch: handler returned non-JSON for issue #{issue_num}: "
            f"stdout={stdout[:200]!r} stderr={stderr[:200]!r}"
        ) from exc

    status = result.get("status")

    if status == "approval_required":
        # Gate fired — create a draft and post the approval card so a human can
        # approve.  Worker is NOT spawned here; the caller must not enrol in
        # awaiting (it checks the return value of _dispatch_worker indirectly
        # by catching exceptions — approval_required is NOT an error).
        gate_preview = result.get("preview") or {}
        budget_seconds = int(payload.get("worker_budget_seconds") or 1800)
        persona = payload.get("worker_persona", "dev") or "dev"
        create_fn = _create_draft_fn or _default_create_draft_fn
        await create_fn(
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            issue_url=issue_url,
            issue_num=issue_num,
            issue_title=issue_title,
            model=model,
            persona=persona,
            budget_seconds=budget_seconds,
            gate_preview=gate_preview,
        )
        logger.info(
            "auto_dispatch: approval_required for issue #%s (gate_reason=%r); draft posted — awaiting human Approve",
            issue_num,
            gate_preview.get("gate_reason"),
        )
        return "approval_required"

    if status != "launched":
        raise RuntimeError(
            f"auto_dispatch: dispatch_issue for issue #{issue_num} returned status={status!r} "
            f"detail={result.get('reason') or result.get('detail')!r}"
        )
    logger.info(
        "auto_dispatch: launched worker dispatch %s for issue #%s",
        result.get("dispatch_id"),
        issue_num,
    )
    return "launched"


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def register_auto_dispatch(
    store: Any,
    *,
    agent_name: str,
    repo: str,
    pat_path: str = MERGE_PAT_PATH,
    destination: str | None = None,
    period_seconds: int = DEFAULT_PERIOD_SECONDS,
    counter_path: str = DEFAULT_COUNTER_PATH,
) -> Any:
    """Idempotently register the auto-dispatch system task in *store*.

    Safe to call on every router boot — if the task already exists under
    :data:`CALLABLE_REF` it is returned untouched rather than duplicated.
    """
    existing = store.list_by_callable_ref(CALLABLE_REF)
    if existing:
        logger.debug("auto_dispatch: system task already registered (%s)", existing[0].task_id)
        return existing[0]

    payload: dict[str, Any] = {
        "repo": repo,
        "pat_path": pat_path,
        "counter_path": counter_path,
    }
    if destination:
        payload["destination"] = destination

    task = store.create_system_task(
        agent_name=agent_name,
        name=TASK_NAME,
        callable_ref=CALLABLE_REF,
        payload=payload,
        period_seconds=period_seconds,
    )
    logger.info(
        "auto_dispatch: registered system task task_id=%s repo=%s period=%ss agent=%s",
        task.task_id,
        repo,
        period_seconds,
        agent_name,
    )
    return task
