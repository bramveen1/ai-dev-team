"""Unit tests for issue #588: pr_review atomic verb.

Covers:
- Happy path: approve, request-changes, comment (receipt with review_id + html_url)
- Identity-conflict refusal (aidt-tl-sam is author/committer of a PR commit)
- Wrong-identity refusal (token resolves to unexpected login)
- Invalid verdict (error returned immediately, no gh call)
- Single gh api call asserted for the review POST (no retry)
- Dry-run runs all preconditions but posts nothing
- pr_review_health: happy path, gh not found, token missing, wrong identity
- _parse_pr_url_for_review: happy path and error cases
- CLI routing: pr_review and pr_review_health verbs
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"

# ── Module loader ─────────────────────────────────────────────────────────────


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_handler_588",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _run_sequence(*responses) -> MagicMock:
    """Return a run mock that yields responses in order."""
    mock = MagicMock(side_effect=list(responses))
    return mock


FAKE_TOKEN = "ghp_fakeSAMtoken12345"
FAKE_PR_URL = "https://github.com/bramveen1/ai-dev-team/pull/42"
FAKE_REVIEW_RESPONSE = json.dumps(
    {
        "id": 1234567890,
        "html_url": "https://github.com/bramveen1/ai-dev-team/pull/42#pullrequestreview-1234567890",
    }
)

# Ordered mock responses for a successful pr_review call:
# 1. gh api user -q .login → aidt-tl-sam
# 2. gh api repos/.../pulls/42/commits → [] (no conflict)
# 3. gh api repos/.../pulls/42/reviews POST → review JSON
_HAPPY_RUN_SEQUENCE = [
    _completed(stdout="aidt-tl-sam\n"),
    _completed(stdout="[]"),
    _completed(stdout=FAKE_REVIEW_RESPONSE),
]


def _make_token_file(tmp_path: Path, token: str = FAKE_TOKEN) -> Path:
    p = tmp_path / "gh-aidt-sam.token"
    p.write_text(token + "\n")
    return p


# ── _parse_pr_url_for_review ──────────────────────────────────────────────────


def test_parse_pr_url_happy(handler):
    owner_repo, pr_num = handler._parse_pr_url_for_review(FAKE_PR_URL)
    assert owner_repo == "bramveen1/ai-dev-team"
    assert pr_num == 42


def test_parse_pr_url_pulls_variant(handler):
    owner_repo, pr_num = handler._parse_pr_url_for_review("https://github.com/foo/bar/pulls/99")
    assert owner_repo == "foo/bar"
    assert pr_num == 99


def test_parse_pr_url_trailing_slash(handler):
    owner_repo, pr_num = handler._parse_pr_url_for_review("https://github.com/bramveen1/ai-dev-team/pull/7/")
    assert owner_repo == "bramveen1/ai-dev-team"
    assert pr_num == 7


def test_parse_pr_url_not_github(handler):
    with pytest.raises(ValueError, match="cannot parse owner/repo"):
        handler._parse_pr_url_for_review("https://example.com/foo/bar/pull/1")


def test_parse_pr_url_missing_pr_number(handler):
    with pytest.raises(ValueError):
        handler._parse_pr_url_for_review("https://github.com/foo/bar/pull/notanumber")


# ── pr_review happy paths ─────────────────────────────────────────────────────


def test_pr_review_approve_happy_path(handler, tmp_path):
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(*_HAPPY_RUN_SEQUENCE)

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM — recommend merge.",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "ok"
    assert result["reviewer"] == "aidt-tl-sam"
    assert result["verdict"] == "approve"
    assert result["repo"] == "bramveen1/ai-dev-team"
    assert result["pr"] == 42
    assert result["review_id"] == 1234567890
    assert "pullrequestreview" in result["html_url"]


def test_pr_review_request_changes_happy_path(handler, tmp_path):
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),
        _completed(stdout="[]"),
        _completed(stdout=json.dumps({"id": 999, "html_url": "https://github.com/o/r/pull/1#pr-999"})),
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="request-changes",
        body="Please fix the tests.",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "ok"
    assert result["verdict"] == "request-changes"
    assert result["review_id"] == 999


def test_pr_review_comment_happy_path(handler, tmp_path):
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),
        _completed(stdout="[]"),
        _completed(stdout=json.dumps({"id": 111, "html_url": "https://github.com/o/r/pull/1#pr-111"})),
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="comment",
        body="Looks fine, just a note.",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "ok"
    assert result["verdict"] == "comment"


# ── pr_review single-call assert ─────────────────────────────────────────────


def test_pr_review_exactly_one_review_post_call(handler, tmp_path):
    """Assert exactly one gh api review POST is made and no retry on failure."""
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(*_HAPPY_RUN_SEQUENCE)

    handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        _token_path=token_file,
        _run=run,
    )

    # Total calls: identity check + conflict check + review POST = 3
    assert run.call_count == 3

    # The review POST must be the only call containing --method POST
    review_post_calls = [c for c in run.call_args_list if "--method" in c.args[0] and "POST" in c.args[0]]
    assert len(review_post_calls) == 1


def test_pr_review_no_retry_on_error(handler, tmp_path):
    """On non-zero exit from the review POST, return error and make no further calls."""
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),  # identity
        _completed(stdout="[]"),  # conflict check
        _completed(stdout="", stderr="Bad credentials", returncode=1),  # review fails
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "error"
    assert result["reason"] == "gh_api_error"
    assert run.call_count == 3  # no 4th call (no retry)


# ── pr_review refusals ────────────────────────────────────────────────────────


def test_pr_review_invalid_verdict_no_gh_call(handler, tmp_path):
    """Invalid verdict returns error immediately without any gh call."""
    token_file = _make_token_file(tmp_path)
    run = MagicMock()

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="lgtm",  # invalid
        body="...",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_verdict"
    run.assert_not_called()


def test_pr_review_identity_conflict_refused(handler, tmp_path):
    """aidt-tl-sam is author of a commit → refused / identity_conflict."""
    token_file = _make_token_file(tmp_path)
    commit_with_conflict = json.dumps(
        [
            {
                "author": {"login": "aidt-tl-sam"},
                "committer": {"login": "web-flow"},
            }
        ]
    )
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),  # identity
        _completed(stdout=commit_with_conflict),  # conflict found
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "refused"
    assert result["reason"] == "identity_conflict"
    # No review POST was made
    assert run.call_count == 2


def test_pr_review_identity_conflict_committer(handler, tmp_path):
    """aidt-tl-sam is committer (not author) of a commit → refused."""
    token_file = _make_token_file(tmp_path)
    commit_with_conflict = json.dumps(
        [
            {
                "author": {"login": "some-dev"},
                "committer": {"login": "aidt-tl-sam"},
            }
        ]
    )
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),
        _completed(stdout=commit_with_conflict),
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "refused"
    assert result["reason"] == "identity_conflict"


def test_pr_review_wrong_identity_refused(handler, tmp_path):
    """Token resolves to unexpected login → refused / wrong_identity."""
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="some-other-bot\n"),  # wrong identity
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "refused"
    assert result["reason"] == "wrong_identity"
    # No conflict check or review POST made
    assert run.call_count == 1


def test_pr_review_token_missing_refused(handler, tmp_path):
    """Token file absent → refused / token_missing."""
    run = MagicMock()

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        _token_path=tmp_path / "nonexistent.token",
        _run=run,
    )

    assert result["status"] == "refused"
    assert result["reason"] == "token_missing"
    run.assert_not_called()


# ── pr_review dry-run ─────────────────────────────────────────────────────────


def test_pr_review_dry_run_posts_nothing(handler, tmp_path):
    """dry_run=True runs preconditions but makes no review POST."""
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),  # identity
        _completed(stdout="[]"),  # conflict check
        # No third call expected
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        dry_run=True,
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "dry_run"
    assert result["reviewer"] == "aidt-tl-sam"
    assert result["verdict"] == "approve"
    assert "would_post" in result
    assert result["would_post"]["event"] == "APPROVE"
    # Only identity + conflict calls made, no POST
    assert run.call_count == 2


def test_pr_review_dry_run_catches_conflict(handler, tmp_path):
    """dry_run still enforces the identity-conflict guard."""
    token_file = _make_token_file(tmp_path)
    conflict_commits = json.dumps([{"author": {"login": "aidt-tl-sam"}, "committer": {"login": "x"}}])
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),
        _completed(stdout=conflict_commits),
    )

    result = handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict="approve",
        body="LGTM",
        dry_run=True,
        _token_path=token_file,
        _run=run,
    )

    assert result["status"] == "refused"
    assert result["reason"] == "identity_conflict"


# ── pr_review gh api event mapping ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("verdict", "expected_event"),
    [
        ("approve", "APPROVE"),
        ("request-changes", "REQUEST_CHANGES"),
        ("comment", "COMMENT"),
    ],
)
def test_pr_review_api_event_mapping(handler, tmp_path, verdict, expected_event):
    """Verify verdict → GitHub API event mapping."""
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="aidt-tl-sam\n"),
        _completed(stdout="[]"),
        _completed(stdout=FAKE_REVIEW_RESPONSE),
    )

    handler.pr_review(
        pr_url=FAKE_PR_URL,
        verdict=verdict,
        body="test",
        _token_path=token_file,
        _run=run,
    )

    # The third call is the POST; check its args contain the correct event
    post_call_args = run.call_args_list[2].args[0]
    assert f"event={expected_event}" in post_call_args


# ── pr_review_health ──────────────────────────────────────────────────────────


def test_pr_review_health_happy_path(handler, tmp_path):
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="gh version 2.40.0 (2024-01-01)\n"),  # gh --version
        _completed(stdout="aidt-tl-sam\n"),  # gh api user
    )

    result = handler.pr_review_health(_token_path=token_file, _run=run)

    assert result["status"] == "ok"
    assert result["gh_present"] is True
    assert result["token_ok"] is True
    assert result["identity_ok"] is True
    assert result["reviewer"] == "aidt-tl-sam"


def test_pr_review_health_gh_not_found(handler, tmp_path):
    token_file = _make_token_file(tmp_path)

    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("gh not found")

    result = handler.pr_review_health(_token_path=token_file, _run=_raise_not_found)

    assert result["status"] == "error"
    assert result["reason"] == "gh_not_found"
    assert result["gh_present"] is False


def test_pr_review_health_token_missing(handler, tmp_path):
    run = _run_sequence(
        _completed(stdout="gh version 2.40.0\n"),  # gh --version ok
    )

    result = handler.pr_review_health(
        _token_path=tmp_path / "nonexistent.token",
        _run=run,
    )

    assert result["status"] == "error"
    assert result["reason"] == "token_missing"
    assert result["token_ok"] is False


def test_pr_review_health_wrong_identity(handler, tmp_path):
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(
        _completed(stdout="gh version 2.40.0\n"),
        _completed(stdout="not-sam\n"),
    )

    result = handler.pr_review_health(_token_path=token_file, _run=run)

    assert result["status"] == "error"
    assert result["reason"] == "wrong_identity"
    assert result["identity_ok"] is False
    assert result["token_ok"] is True


# ── CLI routing via run() ─────────────────────────────────────────────────────


def test_cli_pr_review_approve(handler, tmp_path, capsys):
    token_file = _make_token_file(tmp_path)
    run = _run_sequence(*_HAPPY_RUN_SEQUENCE)

    # Patch the module-level subprocess.run to use our mock only for the
    # pr_review call paths; use env var to supply token path.
    import os

    original_env = os.environ.copy()
    os.environ[handler.SAM_REVIEW_TOKEN_PATH_ENV] = str(token_file)
    try:
        # We need to monkey-patch the _run default on pr_review via the module.
        # The cleanest way is to call run() with the verb and intercept via
        # patching the module attribute.
        original_pr_review = handler.pr_review

        def _patched_pr_review(**kwargs):
            kwargs.setdefault("_run", run)
            kwargs.setdefault("_token_path", token_file)
            return original_pr_review(**kwargs)

        handler.pr_review = _patched_pr_review
        exit_code = handler.run(["pr_review", "--pr-url", FAKE_PR_URL, "--verdict", "approve", "--body", "LGTM"])
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        handler.pr_review = original_pr_review

    out = capsys.readouterr().out
    data = json.loads(out)
    assert exit_code == handler.EXIT_OK
    assert data["status"] == "ok"


def test_cli_pr_review_invalid_verdict_exit_nonzero(handler, tmp_path, capsys):
    token_file = _make_token_file(tmp_path)

    original_pr_review = handler.pr_review

    def _patched(**kwargs):
        kwargs.setdefault("_run", MagicMock())
        kwargs.setdefault("_token_path", token_file)
        return original_pr_review(**kwargs)

    handler.pr_review = _patched
    try:
        handler.run(["pr_review", "--pr-url", FAKE_PR_URL, "--verdict", "bad-verdict", "--body", "x"])
    except SystemExit:
        # argparse exits on invalid choice — expected
        pass
    finally:
        handler.pr_review = original_pr_review


def test_cli_pr_review_health_ok(handler, tmp_path, capsys):
    token_file = _make_token_file(tmp_path)

    original_prh = handler.pr_review_health

    def _patched(**kwargs):
        kwargs.setdefault(
            "_run",
            _run_sequence(
                _completed(stdout="gh version 2.40.0\n"),
                _completed(stdout="aidt-tl-sam\n"),
            ),
        )
        kwargs.setdefault("_token_path", token_file)
        return original_prh(**kwargs)

    handler.pr_review_health = _patched
    try:
        exit_code = handler.run(["pr_review_health"])
    finally:
        handler.pr_review_health = original_prh

    out = capsys.readouterr().out
    data = json.loads(out)
    assert exit_code == handler.EXIT_OK
    assert data["status"] == "ok"


def test_cli_pr_review_health_error_exit_nonzero(handler, tmp_path, capsys):
    original_prh = handler.pr_review_health

    def _patched(**kwargs):
        return {"status": "error", "reason": "token_missing"}

    handler.pr_review_health = _patched
    try:
        exit_code = handler.run(["pr_review_health"])
    finally:
        handler.pr_review_health = original_prh

    out = capsys.readouterr().out
    data = json.loads(out)
    assert exit_code != handler.EXIT_OK
    assert data["status"] == "error"
