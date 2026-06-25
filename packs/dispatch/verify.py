"""verify.py — Verify-then-file primitives (issue #418).

Two sub-verbs exposed via handler.py:

  ``verify.issue_create``
      Creates an issue, then confirms it appeared in a fresh ``gh issue
      list`` (the consumer surface a human or board sees).  Only returns
      the API-reported number + URL after the list readback succeeds.

  ``verify.pr_review``
      Submits a PR review, then confirms the review event is present in
      ``gh pr view --json reviews``.  Only returns success after the
      readback finds our event with the expected state.

Design contract
---------------
* **Verify via the consumer surface, not the confirming one.**  The
  ``gh issue create`` / ``gh pr review`` 200 is never trusted as proof —
  a separate ``gh issue list`` / ``gh pr view`` query is the oracle.
* **Receipts-or-it-didn't-happen.**  Raw gh stdout/stderr (exit code +
  key fields) are captured and returned in every result so the caller's
  message must quote real output, not a paraphrase.
* **Fail loud on 403/429/4xx.**  Any non-zero gh exit that carries an
  HTTP error code raises ``GhApiError``; no success path on a
  rate-limited or failed readback.
* **Eventual-consistency.**  Bounded retry (``max_retries``, default 3)
  with a short backoff before raising ``UnverifiedSideEffect``.

Rollback
--------
Remove the deny rule from Sam's ``~/.claude/settings.json`` (see
``config.example/agents/sam/settings.json``).  Without the deny rule
this module is still importable and callable; the verbs just become
opt-in rather than the only path.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any


class GhApiError(RuntimeError):
    """Raised when ``gh`` exits non-zero due to an HTTP error (403, 429, …).

    Attributes:
        status: HTTP status code extracted from gh stderr, or the raw exit
                code when no HTTP status can be parsed.
        stderr: raw stderr text from the gh invocation.
        stdout: raw stdout text (may be empty for error responses).
    """

    def __init__(self, status: int, stderr: str, stdout: str = "") -> None:
        self.status = status
        self.stderr = stderr
        self.stdout = stdout
        detail = (stderr or stdout or "(no detail)").splitlines()[0][:200]
        super().__init__(f"gh API error {status}: {detail!r}")


class UnverifiedSideEffect(RuntimeError):
    """Raised when the consumer-surface readback cannot confirm the side-effect.

    This is the core anti-fabrication guard: a create or review that is
    absent from the list/view readback must be treated as unconfirmed
    rather than optimistically reported as done.
    """


_HTTP_STATUS_RE = re.compile(r"HTTP[_/ ](\d{3})")


def _extract_http_status(text: str) -> int | None:
    m = _HTTP_STATUS_RE.search(text or "")
    return int(m.group(1)) if m else None


def _check_gh_error(returncode: int, stdout: str, stderr: str) -> None:
    """Raise ``GhApiError`` when *returncode* is non-zero."""
    if returncode == 0:
        return
    status = _extract_http_status(stderr) or _extract_http_status(stdout)
    raise GhApiError(status or returncode, stderr, stdout)


def _run(cmd: list[str], *, run: Any = None) -> tuple[int, str, str]:
    """Run *cmd* and return ``(returncode, stdout, stderr)``.

    Converts ``TimeoutExpired`` to ``GhApiError`` so the caller never
    has to catch subprocess exceptions directly.

    ``run`` defaults to ``subprocess.run`` looked up at call time so
    that test monkeypatching of ``subprocess.run`` is effective.
    """
    _run_fn = run if run is not None else subprocess.run
    try:
        result = _run_fn(cmd, capture_output=True, text=True, check=False, timeout=30)
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        raise GhApiError(0, f"gh timed out: {cmd!r}")


def verify_issue_create(
    repo: str,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    run: Any = None,
    max_retries: int = 3,
    backoff_s: float = 1.0,
    _sleep_fn: Any = None,
) -> dict[str, Any]:
    """Create a GitHub issue and verify it via ``gh issue list`` readback.

    Args:
        repo: ``owner/repo`` slug (e.g. ``bramveen1/ai-dev-team``).
        title: issue title.
        body: issue body markdown.
        labels: optional list of label names to apply.
        run: injectable subprocess.run replacement for tests.
        max_retries: readback attempts before raising (default 3).
        backoff_s: seconds to sleep between readback attempts.
        _sleep_fn: injectable sleep function for tests.

    Returns::

        {
          "status": "verified",
          "number": <int — API-reported issue number>,
          "url": <str — API-reported issue URL>,
          "receipts": {
            "create_stdout": ..., "create_stderr": ..., "create_exit": ...,
            "list_stdout":   ..., "list_stderr":   ..., "list_exit": ...,
          }
        }

    Raises:
        GhApiError: gh returned 403/429/4xx on create or readback.
        UnverifiedSideEffect: issue absent from list after all retries.
    """
    sleep = _sleep_fn if _sleep_fn is not None else time.sleep

    # ── Step 1: create ────────────────────────────────────────────────────
    create_cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
    ]
    if labels:
        for label in labels:
            create_cmd.extend(["--label", label])

    rc, stdout, stderr = _run(create_cmd, run=run)
    receipts: dict[str, Any] = {
        "create_stdout": stdout,
        "create_stderr": stderr,
        "create_exit": rc,
    }
    _check_gh_error(rc, stdout, stderr)

    # gh issue create prints the new issue URL to stdout (no --json flag available)
    _issue_url_re = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)")
    m = _issue_url_re.search(stdout)
    if not m:
        raise UnverifiedSideEffect(f"Cannot parse issue URL from gh issue create stdout={stdout!r}")
    api_number = int(m.group(1))
    api_url = m.group(0)

    # ── Step 2: verify via list (consumer surface) ────────────────────────
    # Use --state open so newly created issues are always in scope.
    # --limit 100 covers busy repos while keeping the call fast.
    list_cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--json",
        "number,url",
        "--limit",
        "100",
    ]

    for attempt in range(max_retries):
        if attempt > 0:
            sleep(backoff_s)

        rc_l, stdout_l, stderr_l = _run(list_cmd, run=run)
        receipts["list_stdout"] = stdout_l
        receipts["list_stderr"] = stderr_l
        receipts["list_exit"] = rc_l
        _check_gh_error(rc_l, stdout_l, stderr_l)

        try:
            issues = json.loads(stdout_l.strip() or "[]")
            if not isinstance(issues, list):
                issues = []
        except json.JSONDecodeError:
            issues = []

        if any(isinstance(i, dict) and int(i.get("number", -1)) == api_number for i in issues):
            return {
                "status": "verified",
                "number": api_number,
                "url": api_url,
                "receipts": receipts,
            }

    raise UnverifiedSideEffect(
        f"Issue #{api_number} not found in list readback after {max_retries} attempt(s). create_stdout={stdout!r}"
    )


def verify_pr_review(
    repo: str,
    pr: int | str,
    body: str,
    *,
    event: str = "COMMENT",
    run: Any = None,
    max_retries: int = 3,
    backoff_s: float = 1.0,
    _sleep_fn: Any = None,
) -> dict[str, Any]:
    """Submit a PR review and verify it appears in ``gh pr view --json reviews``.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number (int or str).
        body: review comment text.
        event: one of ``"COMMENT"``, ``"APPROVE"``, ``"REQUEST_CHANGES"``.
        run: injectable subprocess.run replacement for tests.
        max_retries: readback attempts before raising (default 3).
        backoff_s: seconds to sleep between readback attempts.
        _sleep_fn: injectable sleep function for tests.

    Returns::

        {
          "status": "verified",
          "event": <normalised event str>,
          "receipts": {
            "review_stdout": ..., "review_stderr": ..., "review_exit": ...,
            "view_stdout":   ..., "view_stderr":   ..., "view_exit": ...,
          }
        }

    Raises:
        GhApiError: gh returned 403/429/4xx on submit or readback.
        UnverifiedSideEffect: review absent from pr-view after all retries.
        ValueError: unrecognised *event* value.
    """
    sleep = _sleep_fn if _sleep_fn is not None else time.sleep

    _FLAG_MAP = {
        "COMMENT": "--comment",
        "APPROVE": "--approve",
        "REQUEST_CHANGES": "--request-changes",
    }
    _STATE_MAP = {
        "COMMENT": "COMMENTED",
        "APPROVE": "APPROVED",
        "REQUEST_CHANGES": "CHANGES_REQUESTED",
    }

    event_upper = event.upper()
    if event_upper not in _FLAG_MAP:
        raise ValueError(f"Unknown event {event!r}; must be one of {sorted(_FLAG_MAP)}")

    gh_flag = _FLAG_MAP[event_upper]
    expected_state = _STATE_MAP[event_upper]

    # ── Pre-read: count existing reviews ─────────────────────────────────
    view_cmd = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "reviews"]
    rc_pre, stdout_pre, stderr_pre = _run(view_cmd, run=run)
    _check_gh_error(rc_pre, stdout_pre, stderr_pre)

    try:
        pre_reviews: list = json.loads(stdout_pre.strip() or "{}").get("reviews", []) or []
    except (json.JSONDecodeError, AttributeError):
        pre_reviews = []
    pre_count = len(pre_reviews)

    # ── Submit the review ─────────────────────────────────────────────────
    review_cmd = [
        "gh",
        "pr",
        "review",
        str(pr),
        "--repo",
        repo,
        gh_flag,
        "--body",
        body,
    ]
    rc_rv, stdout_rv, stderr_rv = _run(review_cmd, run=run)
    receipts: dict[str, Any] = {
        "review_stdout": stdout_rv,
        "review_stderr": stderr_rv,
        "review_exit": rc_rv,
    }
    _check_gh_error(rc_rv, stdout_rv, stderr_rv)

    # ── Verify via pr view (consumer surface) ────────────────────────────
    for attempt in range(max_retries):
        if attempt > 0:
            sleep(backoff_s)

        rc_v, stdout_v, stderr_v = _run(view_cmd, run=run)
        receipts["view_stdout"] = stdout_v
        receipts["view_stderr"] = stderr_v
        receipts["view_exit"] = rc_v
        _check_gh_error(rc_v, stdout_v, stderr_v)

        try:
            post_reviews: list = json.loads(stdout_v.strip() or "{}").get("reviews", []) or []
        except (json.JSONDecodeError, AttributeError):
            post_reviews = []

        count_grew = len(post_reviews) > pre_count
        has_state = any(isinstance(r, dict) and r.get("state") == expected_state for r in post_reviews)

        if count_grew and has_state:
            return {
                "status": "verified",
                "event": event_upper,
                "receipts": receipts,
            }

    raise UnverifiedSideEffect(f"PR #{pr} {event_upper} review not found in readback after {max_retries} attempt(s).")
