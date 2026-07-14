"""pr_review verbs — atomic PR review + health probe (issue #588).

Moved out of handler.py (refactoring roadmap §2a, wave 3b). handler.py
re-exports every name here under the same names, so the CLI surface
(``python handler.py pr_review …``) and the test suite's direct calls
keep working unchanged.

The reviewer PAT path and GitHub identity are CONFIG, not code: they come
from the ``pr_review:`` block in dispatch.yaml (/config/dispatch.yaml in
the container) so a renamed review agent is a config edit, not a code
change. Env vars override for tests and one-off runs; PR_REVIEW_TOKEN_PATH
is preferred, SAM_REVIEW_TOKEN_PATH remains accepted as a legacy alias.
Unconfigured → the verb refuses fail-closed with remediation text.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

# Shared gh-CLI run/error/parse helper (#736) — co-located with this file.
from gh_cli import run_gh, run_gh_json

# Shared PAT/secrets-file reader (#718) — co-located with this file.
from token_reader import read_token_file

# Quota module carries the dispatch.yaml loader — co-located with this file;
# falls back gracefully if quota.py is absent during a zero-downtime upgrade.
try:
    import quota as _quota

    _QUOTA_AVAILABLE = True
except ImportError:
    _quota = None  # type: ignore[assignment]
    _QUOTA_AVAILABLE = False

# Same resolution as handler._QUOTA_CONFIG_PATH: /config/dispatch.yaml in the
# deployed container, <repo-root>/dispatch.yaml in dev, QUOTA_CONFIG_PATH
# override for tests.
_QUOTA_CONFIG_PATH = (
    Path(os.environ["QUOTA_CONFIG_PATH"])
    if "QUOTA_CONFIG_PATH" in os.environ
    else Path(__file__).parent.parent.parent / "dispatch.yaml"
)

PR_REVIEW_TOKEN_PATH_ENV = "PR_REVIEW_TOKEN_PATH"
SAM_REVIEW_TOKEN_PATH_ENV = "SAM_REVIEW_TOKEN_PATH"  # legacy alias, one release
PR_REVIEW_IDENTITY_ENV = "PR_REVIEW_IDENTITY"

# The three allowed review verdicts and their GitHub API event names.
PR_REVIEW_VERDICTS = frozenset({"approve", "request-changes", "comment"})
_PR_REVIEW_EVENT_MAP: dict[str, str] = {
    "approve": "APPROVE",
    "request-changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}

_PR_REVIEW_UNCONFIGURED_DETAIL = (
    "pr_review is not configured — add a pr_review: {token_path, identity} block "
    "to /config/dispatch.yaml (or set PR_REVIEW_TOKEN_PATH / PR_REVIEW_IDENTITY)"
)


def resolve_settings(*, quota_mod: Any = None, config_path: Path | None = None) -> dict[str, Any]:
    """Resolve pr_review config: env override → dispatch.yaml ``pr_review:`` → unset.

    Returns ``{"token_path": str|None, "identity": str|None}``. ``None`` means
    unconfigured — callers refuse fail-closed with remediation text.

    ``quota_mod``/``config_path`` default to this module's own quota import
    and config-path resolution; handler.py's ``_pr_review_settings`` wrapper
    forwards its module globals so existing test patch targets keep working.
    """
    if quota_mod is None and _QUOTA_AVAILABLE:
        quota_mod = _quota
    if config_path is None:
        config_path = _QUOTA_CONFIG_PATH
    file_cfg: dict[str, Any] = {"token_path": None, "identity": None}
    if quota_mod is not None:
        file_cfg = quota_mod.load_pr_review_config(config_path)
    token_path = (
        os.environ.get(PR_REVIEW_TOKEN_PATH_ENV)
        or os.environ.get(SAM_REVIEW_TOKEN_PATH_ENV)
        or file_cfg.get("token_path")
    )
    identity = os.environ.get(PR_REVIEW_IDENTITY_ENV) or file_cfg.get("identity")
    return {"token_path": token_path, "identity": identity}


def _read_review_token(path: str | Path | None) -> str | None:
    """Read the reviewer GitHub PAT from the secrets file at ``path``.

    Skips comment lines and blank lines. Returns None when the path is
    unconfigured or the file is absent, unreadable, or contains only
    comments/blanks. Delegates to the shared ``token_reader.read_token_file``
    helper (#718).
    """
    if path is None:
        return None
    return read_token_file(path, skip_comments=True, raise_on_empty=False)


def _parse_pr_url_for_review(pr_url: str) -> tuple[str, int]:
    """Parse (owner/repo, pr_number) from a GitHub PR URL.

    Raises ValueError on malformed input.
    """
    gh_prefix = "github.com/"
    idx = pr_url.find(gh_prefix)
    if idx == -1:
        raise ValueError(f"cannot parse owner/repo from pr_url: {pr_url!r}")
    parts = pr_url[idx + len(gh_prefix) :].rstrip("/").split("/")
    if len(parts) < 4 or parts[2] not in ("pull", "pulls") or not parts[3].isdigit():
        raise ValueError(f"cannot parse owner/repo/pr_number from pr_url: {pr_url!r}")
    return f"{parts[0]}/{parts[1]}", int(parts[3])


def _verify_review_identity(token: str, *, run: Any = subprocess.run) -> tuple[str | None, str | None]:
    """Verify the token resolves to the configured reviewer via gh api user.

    Returns (login, error_detail). On success, error_detail is None.
    Token is env-scoped to the gh subprocess only.
    """
    result = run_gh(
        ["gh", "api", "user", "-q", ".login"],
        timeout=10,
        run=run,
        env={**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token},
    )
    if result.error_kind is not None:
        return None, result.error

    if not result.ok:
        return None, result.error_detail(tail=200) or "gh api user returned non-zero"

    login = result.stdout.strip()
    return login or None, None


def _check_pr_identity_conflict(
    owner_repo: str,
    pr_num: int,
    token: str,
    identity: str,
    *,
    run: Any = subprocess.run,
) -> bool:
    """Return True if ``identity`` authored or committed any commit on the PR.

    Fetches all PR commits via gh api and checks author/committer logins.
    Returns False on any API failure so the caller can surface a distinct
    error rather than silently blocking all reviews.
    """
    result, commits = run_gh_json(
        ["gh", "api", f"repos/{owner_repo}/pulls/{pr_num}/commits", "--paginate"],
        timeout=15,
        run=run,
        env={**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token},
    )
    if not result.ok or not isinstance(commits, list):
        return False

    for commit in commits:
        author_login = (commit.get("author") or {}).get("login") or ""
        committer_login = (commit.get("committer") or {}).get("login") or ""
        if identity in (author_login, committer_login):
            return True
    return False


def pr_review(
    *,
    pr_url: str,
    verdict: str,
    body: str,
    dry_run: bool = False,
    _token_path: str | Path | None = None,
    _run: Any = subprocess.run,
    _settings_fn: Any = None,
) -> dict[str, Any]:
    """Post a formal GitHub PR review as the configured reviewer identity.

    Reviewer identity + PAT path come from dispatch.yaml's ``pr_review:``
    block (env overridable — see :func:`resolve_settings`). Single-shot:
    exactly one gh api call for the review POST, no retry. The reviewer
    token is env-scoped to the gh subprocess only, never exported.

    Returns a receipt dict on success:
        {status: ok, reviewer, verdict, repo, pr, review_id, html_url}

    On refusal or error:
        {status: refused|error, reason, detail}
    """
    # Validate verdict before any I/O.
    if verdict not in PR_REVIEW_VERDICTS:
        return {
            "status": "error",
            "reason": "invalid_verdict",
            "detail": f"verdict must be one of {sorted(PR_REVIEW_VERDICTS)!r}; got {verdict!r}",
        }

    # Parse PR URL.
    try:
        owner_repo, pr_num = _parse_pr_url_for_review(pr_url)
    except ValueError as e:
        return {"status": "error", "reason": "invalid_pr_url", "detail": str(e)}

    # Resolve reviewer config — unconfigured refuses fail-closed.
    cfg = _settings_fn() if _settings_fn is not None else resolve_settings()
    identity = cfg["identity"]
    if not identity:
        return {"status": "refused", "reason": "unconfigured", "detail": _PR_REVIEW_UNCONFIGURED_DETAIL}

    # Load the reviewer token.
    token_path = _token_path if _token_path is not None else cfg["token_path"]
    token = _read_review_token(token_path)
    if not token:
        token_source = str(token_path) if token_path else "pr_review.token_path (unconfigured)"
        return {
            "status": "refused",
            "reason": "token_missing",
            "detail": f"token not found at {token_source}",
        }

    # Verify identity.
    login, id_error = _verify_review_identity(token, run=_run)
    if login != identity:
        detail = f"expected login {identity!r}, got {login!r}"
        if id_error:
            detail += f": {id_error}"
        return {"status": "refused", "reason": "wrong_identity", "detail": detail}

    # Identity conflict guard — fail-closed: if the reviewer identity is an
    # author/committer of any commit on this PR, refuse to review it.
    if _check_pr_identity_conflict(owner_repo, pr_num, token, identity, run=_run):
        return {
            "status": "refused",
            "reason": "identity_conflict",
            "detail": (f"{identity} authored or committed a commit on this PR and cannot review it"),
        }

    api_event = _PR_REVIEW_EVENT_MAP[verdict]

    if dry_run:
        return {
            "status": "dry_run",
            "reviewer": identity,
            "verdict": verdict,
            "repo": owner_repo,
            "pr": pr_num,
            "would_post": {
                "endpoint": f"repos/{owner_repo}/pulls/{pr_num}/reviews",
                "event": api_event,
                "body": body,
            },
        }

    # Single-shot review POST — no retry on failure (ref #473).
    sub_env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}
    result, review_data = run_gh_json(
        [
            "gh",
            "api",
            f"repos/{owner_repo}/pulls/{pr_num}/reviews",
            "--method",
            "POST",
            "--field",
            f"body={body}",
            "--field",
            f"event={api_event}",
        ],
        timeout=30,
        run=_run,
        env=sub_env,
    )
    if result.error_kind == "timeout":
        return {"status": "error", "reason": "gh_timeout", "detail": "gh api timed out after 30s"}
    if result.error_kind in ("not_found", "os_error"):
        return {"status": "error", "reason": "gh_not_found", "detail": result.error}

    if not result.ok:
        return {
            "status": "error",
            "reason": "gh_api_error",
            "exit_code": result.returncode,
            "detail": result.error_detail(tail=300),
        }

    if review_data is None:
        return {
            "status": "error",
            "reason": "invalid_response",
            "detail": f"gh api returned non-JSON: {(result.stdout or '')[:200]}",
        }

    return {
        "status": "ok",
        "reviewer": identity,
        "verdict": verdict,
        "repo": owner_repo,
        "pr": pr_num,
        "review_id": review_data.get("id"),
        "html_url": review_data.get("html_url"),
    }


def pr_review_health(
    *,
    _token_path: str | Path | None = None,
    _run: Any = subprocess.run,
    _settings_fn: Any = None,
) -> dict[str, Any]:
    """Smoke probe: verify gh present, token resolves, login matches identity.

    Posts nothing. Returns a health dict safe to surface in the dispatch
    health surface alongside dispatch_health.
    """
    # Check gh present.
    ver_result = run_gh(["gh", "--version"], timeout=5, run=_run)
    if ver_result.error_kind == "not_found":
        return {
            "status": "error",
            "reason": "gh_not_found",
            "gh_present": False,
            "token_ok": False,
            "identity_ok": False,
        }
    gh_present = ver_result.ok
    gh_version = ver_result.stdout.split("\n")[0].strip() if gh_present else None

    if not gh_present:
        return {
            "status": "error",
            "reason": "gh_not_available",
            "gh_present": False,
            "token_ok": False,
            "identity_ok": False,
        }

    # Resolve reviewer config — unconfigured is a distinct health failure.
    cfg = _settings_fn() if _settings_fn is not None else resolve_settings()
    identity = cfg["identity"]
    if not identity:
        return {
            "status": "error",
            "reason": "unconfigured",
            "detail": _PR_REVIEW_UNCONFIGURED_DETAIL,
            "gh_present": gh_present,
            "gh_version": gh_version,
            "token_ok": False,
            "identity_ok": False,
        }

    # Load token.
    token_path = _token_path if _token_path is not None else cfg["token_path"]
    token = _read_review_token(token_path)
    if not token:
        return {
            "status": "error",
            "reason": "token_missing",
            "gh_present": gh_present,
            "gh_version": gh_version,
            "token_ok": False,
            "identity_ok": False,
        }

    # Verify identity — no review posted.
    login, id_error = _verify_review_identity(token, run=_run)
    identity_ok = login == identity

    if not identity_ok:
        detail = f"expected {identity!r}, got {login!r}"
        if id_error:
            detail += f": {id_error}"
        return {
            "status": "error",
            "reason": "wrong_identity",
            "detail": detail,
            "gh_present": gh_present,
            "gh_version": gh_version,
            "token_ok": True,
            "identity_ok": False,
        }

    return {
        "status": "ok",
        "gh_present": gh_present,
        "gh_version": gh_version,
        "token_ok": True,
        "identity_ok": True,
        "reviewer": identity,
    }
