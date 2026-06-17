"""Unit tests for issue #418: verify-then-file primitives.

Covers packs/dispatch/verify.py:
  - verify.issue_create: success, rate-limited create (403), rate-limited
    readback (403), create-then-absent-in-list (fabrication catch),
    empty/garbage create response.
  - verify.pr_review: success (COMMENT), success (APPROVE), rate-limited
    submit (429), pr-review absent (UnverifiedSideEffect).
  - Handler CLI verb routing: verify.issue_create and verify.pr_review
    produce the expected JSON shapes.
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


# ── Loader helpers ────────────────────────────────────────────────────────────


def _load_verify():
    """Load verify.py from the dispatch pack (isolated module load)."""
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_418_verify",
        PACK_DIR / "verify.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_handler():
    """Load handler.py from the dispatch pack (isolated module load)."""
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_418_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def verify():
    return _load_verify()


@pytest.fixture(scope="module")
def handler():
    return _load_handler()


# ── Fake subprocess helpers ───────────────────────────────────────────────────


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _make_run(*responses):
    """Return a fake ``run`` that yields the given _completed() values in order."""
    it = iter(responses)

    def fake_run(cmd, **kwargs):
        return next(it)

    return fake_run


# ─────────────────────────────────────────────────────────────────────────────
# verify_issue_create
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyIssueCreateSuccess:
    """Happy path: create returns a number, list readback finds it."""

    def test_returns_verified_status(self, verify) -> None:
        create_out = json.dumps({"number": 42, "url": "https://github.com/o/r/issues/42"})
        list_out = json.dumps([{"number": 42, "url": "https://github.com/o/r/issues/42"}])

        run = _make_run(_completed(stdout=create_out), _completed(stdout=list_out))
        result = verify.verify_issue_create("o/r", "Title", "Body", run=run, _sleep_fn=lambda s: None)

        assert result["status"] == "verified"

    def test_returns_api_reported_number(self, verify) -> None:
        create_out = json.dumps({"number": 99, "url": "https://github.com/o/r/issues/99"})
        list_out = json.dumps([{"number": 99, "url": "https://github.com/o/r/issues/99"}])

        run = _make_run(_completed(stdout=create_out), _completed(stdout=list_out))
        result = verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

        assert result["number"] == 99

    def test_returns_api_reported_url(self, verify) -> None:
        url = "https://github.com/o/r/issues/55"
        create_out = json.dumps({"number": 55, "url": url})
        list_out = json.dumps([{"number": 55, "url": url}])

        run = _make_run(_completed(stdout=create_out), _completed(stdout=list_out))
        result = verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

        assert result["url"] == url

    def test_receipts_contain_raw_create_output(self, verify) -> None:
        create_out = json.dumps({"number": 1, "url": "https://github.com/o/r/issues/1"})
        list_out = json.dumps([{"number": 1, "url": "https://github.com/o/r/issues/1"}])

        run = _make_run(_completed(stdout=create_out, stderr=""), _completed(stdout=list_out))
        result = verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

        assert "receipts" in result
        assert result["receipts"]["create_stdout"] == create_out
        assert result["receipts"]["create_exit"] == 0

    def test_receipts_contain_raw_list_output(self, verify) -> None:
        create_out = json.dumps({"number": 7, "url": "https://github.com/o/r/issues/7"})
        list_out = json.dumps([{"number": 7, "url": "https://github.com/o/r/issues/7"}])

        run = _make_run(_completed(stdout=create_out), _completed(stdout=list_out))
        result = verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

        assert result["receipts"]["list_stdout"] == list_out

    def test_labels_passed_to_create_command(self, verify) -> None:
        """Labels must be forwarded to the gh issue create invocation."""
        calls: list[list[str]] = []

        create_out = json.dumps({"number": 3, "url": "https://github.com/o/r/issues/3"})
        list_out = json.dumps([{"number": 3, "url": "https://github.com/o/r/issues/3"}])
        responses = iter([_completed(stdout=create_out), _completed(stdout=list_out)])

        def recording_run(cmd, **kwargs):
            calls.append(list(cmd))
            return next(responses)

        verify.verify_issue_create(
            "o/r",
            "T",
            "B",
            labels=["bug", "priority"],
            run=recording_run,
            _sleep_fn=lambda s: None,
        )

        create_cmd = calls[0]
        assert "--label" in create_cmd
        label_indices = [i for i, v in enumerate(create_cmd) if v == "--label"]
        label_values = [create_cmd[i + 1] for i in label_indices]
        assert "bug" in label_values
        assert "priority" in label_values

    def test_eventual_consistency_retry_finds_on_second_attempt(self, verify) -> None:
        """First list attempt returns empty; second attempt finds the issue."""
        sleeps: list[float] = []
        create_out = json.dumps({"number": 10, "url": "https://github.com/o/r/issues/10"})
        list_empty = json.dumps([])
        list_found = json.dumps([{"number": 10, "url": "https://github.com/o/r/issues/10"}])

        run = _make_run(
            _completed(stdout=create_out),
            _completed(stdout=list_empty),
            _completed(stdout=list_found),
        )
        result = verify.verify_issue_create(
            "o/r",
            "T",
            "B",
            run=run,
            _sleep_fn=sleeps.append,
        )

        assert result["status"] == "verified"
        assert len(sleeps) == 1  # one inter-attempt backoff


class TestVerifyIssueCreateRateLimited:
    """403 and 429 on create must raise GhApiError, never claim success."""

    def test_403_on_create_raises_gh_api_error(self, verify) -> None:
        run = _make_run(_completed(returncode=1, stderr="HTTP 403: Forbidden"))
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)
        assert exc_info.value.status == 403

    def test_429_on_create_raises_gh_api_error(self, verify) -> None:
        run = _make_run(_completed(returncode=1, stderr="HTTP 429: rate limited"))
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)
        assert exc_info.value.status == 429

    def test_403_on_list_readback_raises_gh_api_error(self, verify) -> None:
        create_out = json.dumps({"number": 5, "url": "https://github.com/o/r/issues/5"})
        run = _make_run(
            _completed(stdout=create_out),
            _completed(returncode=1, stderr="HTTP 403: Forbidden"),
        )
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)
        assert exc_info.value.status == 403

    def test_gh_api_error_carries_raw_stderr(self, verify) -> None:
        raw_stderr = "HTTP 403: bad credentials — check token"
        run = _make_run(_completed(returncode=1, stderr=raw_stderr))
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)
        assert "403" in str(exc_info.value)


class TestVerifyIssueCreateFabricationCatch:
    """Issue created (200) but absent from list — must raise UnverifiedSideEffect."""

    def test_absent_from_list_raises(self, verify) -> None:
        create_out = json.dumps({"number": 42, "url": "https://github.com/o/r/issues/42"})
        # list returns OTHER issues, not #42
        list_out = json.dumps([{"number": 100, "url": "https://github.com/o/r/issues/100"}])

        run = _make_run(
            _completed(stdout=create_out),
            _completed(stdout=list_out),
            _completed(stdout=list_out),
            _completed(stdout=list_out),
        )
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

    def test_empty_list_raises(self, verify) -> None:
        create_out = json.dumps({"number": 42, "url": "https://github.com/o/r/issues/42"})
        list_empty = json.dumps([])

        run = _make_run(
            _completed(stdout=create_out),
            _completed(stdout=list_empty),
            _completed(stdout=list_empty),
            _completed(stdout=list_empty),
        )
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

    def test_all_retries_exhausted_before_raising(self, verify) -> None:
        """Verify it retries max_retries times before giving up."""
        create_out = json.dumps({"number": 42, "url": "https://github.com/o/r/issues/42"})
        list_empty = json.dumps([])
        sleeps: list[float] = []

        run = _make_run(
            _completed(stdout=create_out),
            _completed(stdout=list_empty),
            _completed(stdout=list_empty),
            _completed(stdout=list_empty),
        )
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_issue_create(
                "o/r",
                "T",
                "B",
                run=run,
                max_retries=3,
                _sleep_fn=sleeps.append,
            )
        assert len(sleeps) == 2  # 3 attempts → 2 inter-attempt sleeps


class TestVerifyIssueCreateGarbageResponse:
    """Garbage / empty create response must raise UnverifiedSideEffect."""

    def test_empty_stdout_raises(self, verify) -> None:
        run = _make_run(_completed(stdout="", returncode=0))
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

    def test_invalid_json_raises(self, verify) -> None:
        run = _make_run(_completed(stdout="not-json", returncode=0))
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)

    def test_missing_number_field_raises(self, verify) -> None:
        run = _make_run(_completed(stdout=json.dumps({"url": "https://example.com"}), returncode=0))
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_issue_create("o/r", "T", "B", run=run, _sleep_fn=lambda s: None)


# ─────────────────────────────────────────────────────────────────────────────
# verify_pr_review
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyPrReviewSuccess:
    """Happy path: review submits and pr view shows the expected state."""

    def _pre_view_out(self, reviews: list) -> str:
        return json.dumps({"reviews": reviews})

    def _post_view_out(self, reviews: list) -> str:
        return json.dumps({"reviews": reviews})

    def test_comment_review_returns_verified(self, verify) -> None:
        pre_view = self._pre_view_out([])
        review_out = ""
        post_view = self._post_view_out([{"state": "COMMENTED", "body": "lgtm"}])

        run = _make_run(
            _completed(stdout=pre_view),  # pre-read
            _completed(stdout=review_out),  # submit
            _completed(stdout=post_view),  # readback
        )
        result = verify.verify_pr_review(
            "o/r",
            7,
            "lgtm",
            event="COMMENT",
            run=run,
            _sleep_fn=lambda s: None,
        )
        assert result["status"] == "verified"
        assert result["event"] == "COMMENT"

    def test_approve_review_returns_verified(self, verify) -> None:
        pre_view = self._pre_view_out([])
        post_view = self._post_view_out([{"state": "APPROVED", "body": "LGTM"}])

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view),
        )
        result = verify.verify_pr_review(
            "o/r",
            3,
            "LGTM",
            event="APPROVE",
            run=run,
            _sleep_fn=lambda s: None,
        )
        assert result["status"] == "verified"
        assert result["event"] == "APPROVE"

    def test_receipts_contain_raw_review_output(self, verify) -> None:
        pre_view = self._pre_view_out([])
        review_stderr = "pr review submitted"
        post_view = self._post_view_out([{"state": "COMMENTED", "body": "x"}])

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout="", stderr=review_stderr),
            _completed(stdout=post_view),
        )
        result = verify.verify_pr_review(
            "o/r",
            1,
            "x",
            event="COMMENT",
            run=run,
            _sleep_fn=lambda s: None,
        )
        assert result["receipts"]["review_stderr"] == review_stderr
        assert result["receipts"]["review_exit"] == 0

    def test_receipts_contain_raw_view_output(self, verify) -> None:
        pre_view = self._pre_view_out([])
        post_view = self._post_view_out([{"state": "COMMENTED", "body": "x"}])

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view),
        )
        result = verify.verify_pr_review(
            "o/r",
            1,
            "x",
            event="COMMENT",
            run=run,
            _sleep_fn=lambda s: None,
        )
        assert result["receipts"]["view_stdout"] == post_view

    def test_event_normalised_to_uppercase(self, verify) -> None:
        pre_view = self._pre_view_out([])
        post_view = self._post_view_out([{"state": "COMMENTED", "body": "x"}])

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view),
        )
        result = verify.verify_pr_review(
            "o/r",
            1,
            "x",
            event="comment",  # lowercase — should be normalised
            run=run,
            _sleep_fn=lambda s: None,
        )
        assert result["event"] == "COMMENT"

    def test_eventual_consistency_retry_finds_on_second_attempt(self, verify) -> None:
        pre_view = self._pre_view_out([])
        post_view_empty = self._post_view_out([])
        post_view_found = self._post_view_out([{"state": "COMMENTED", "body": "x"}])
        sleeps: list[float] = []

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view_empty),
            _completed(stdout=post_view_found),
        )
        result = verify.verify_pr_review(
            "o/r",
            5,
            "x",
            event="COMMENT",
            run=run,
            _sleep_fn=sleeps.append,
        )
        assert result["status"] == "verified"
        assert len(sleeps) == 1


class TestVerifyPrReviewRateLimited:
    """403/429 on submit must raise GhApiError, never claim success."""

    def test_403_on_submit_raises(self, verify) -> None:
        pre_view = json.dumps({"reviews": []})
        run = _make_run(
            _completed(stdout=pre_view),
            _completed(returncode=1, stderr="HTTP 403: Forbidden"),
        )
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_pr_review("o/r", 1, "body", run=run, _sleep_fn=lambda s: None)
        assert exc_info.value.status == 403

    def test_429_on_submit_raises(self, verify) -> None:
        pre_view = json.dumps({"reviews": []})
        run = _make_run(
            _completed(stdout=pre_view),
            _completed(returncode=1, stderr="HTTP 429: too many requests"),
        )
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_pr_review("o/r", 1, "body", run=run, _sleep_fn=lambda s: None)
        assert exc_info.value.status == 429

    def test_403_on_readback_raises(self, verify) -> None:
        pre_view = json.dumps({"reviews": []})
        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),  # submit ok
            _completed(returncode=1, stderr="HTTP 403: Forbidden"),  # readback fails
        )
        with pytest.raises(verify.GhApiError) as exc_info:
            verify.verify_pr_review("o/r", 1, "body", run=run, _sleep_fn=lambda s: None)
        assert exc_info.value.status == 403


class TestVerifyPrReviewAbsent:
    """Review submitted (200) but absent in pr view — must raise UnverifiedSideEffect."""

    def test_review_absent_raises(self, verify) -> None:
        pre_view = json.dumps({"reviews": []})
        # readback: still no reviews even after submit
        post_view = json.dumps({"reviews": []})

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view),
            _completed(stdout=post_view),
            _completed(stdout=post_view),
        )
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_pr_review(
                "o/r",
                1,
                "body",
                run=run,
                max_retries=3,
                _sleep_fn=lambda s: None,
            )

    def test_wrong_state_in_readback_raises(self, verify) -> None:
        """State APPROVED when we expected COMMENTED → absent."""
        pre_view = json.dumps({"reviews": []})
        post_view = json.dumps({"reviews": [{"state": "APPROVED", "body": "x"}]})

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view),
            _completed(stdout=post_view),
            _completed(stdout=post_view),
        )
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_pr_review(
                "o/r",
                1,
                "x",
                event="COMMENT",
                run=run,
                max_retries=3,
                _sleep_fn=lambda s: None,
            )

    def test_all_retries_exhausted_before_raising(self, verify) -> None:
        pre_view = json.dumps({"reviews": []})
        post_view = json.dumps({"reviews": []})
        sleeps: list[float] = []

        run = _make_run(
            _completed(stdout=pre_view),
            _completed(stdout=""),
            _completed(stdout=post_view),
            _completed(stdout=post_view),
            _completed(stdout=post_view),
        )
        with pytest.raises(verify.UnverifiedSideEffect):
            verify.verify_pr_review(
                "o/r",
                1,
                "body",
                run=run,
                max_retries=3,
                _sleep_fn=sleeps.append,
            )
        assert len(sleeps) == 2  # 3 attempts → 2 inter-attempt sleeps


class TestVerifyPrReviewInvalidEvent:
    def test_unknown_event_raises_value_error(self, verify) -> None:
        run = _make_run(_completed(stdout=json.dumps({"reviews": []})))
        with pytest.raises(ValueError, match="Unknown event"):
            verify.verify_pr_review("o/r", 1, "body", event="MERGE", run=run)


# ─────────────────────────────────────────────────────────────────────────────
# Handler CLI verb routing smoke tests
# ─────────────────────────────────────────────────────────────────────────────


class TestHandlerVerifyIssueCreateVerb:
    """handler.run() with verify.issue_create produces expected JSON shape."""

    def test_success_exit_zero(self, handler, monkeypatch) -> None:
        create_out = json.dumps({"number": 5, "url": "https://github.com/o/r/issues/5"})
        list_out = json.dumps([{"number": 5, "url": "https://github.com/o/r/issues/5"}])

        def fake_run(cmd, **kwargs):
            if "create" in cmd:
                return _completed(stdout=create_out)
            return _completed(stdout=list_out)

        import subprocess as _sp

        monkeypatch.setattr(_sp, "run", fake_run)

        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x: captured.append(x))
        rc = handler.run(["verify.issue_create", "--repo", "o/r", "--title", "T", "--body", "B"])

        assert rc == handler.EXIT_OK
        result = json.loads(captured[-1])
        assert result["status"] == "verified"
        assert result["number"] == 5

    def test_rate_limit_exit_nonzero(self, handler, monkeypatch) -> None:
        def fake_run(cmd, **kwargs):
            return _completed(returncode=1, stderr="HTTP 403: Forbidden")

        import subprocess as _sp

        monkeypatch.setattr(_sp, "run", fake_run)

        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x: captured.append(x))
        rc = handler.run(["verify.issue_create", "--repo", "o/r", "--title", "T", "--body", "B"])

        assert rc != handler.EXIT_OK
        result = json.loads(captured[-1])
        assert result["status"] == "error"
        assert result["reason"] == "gh_api_error"
        assert result["http_status"] == 403


class TestHandlerVerifyPrReviewVerb:
    """handler.run() with verify.pr_review produces expected JSON shape."""

    def test_success_exit_zero(self, handler, monkeypatch) -> None:
        pre_view = json.dumps({"reviews": []})
        post_view = json.dumps({"reviews": [{"state": "COMMENTED", "body": "x"}]})
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if "review" in cmd and "--comment" in cmd:
                return _completed(stdout="")
            return _completed(stdout=pre_view if call_count[0] == 1 else post_view)

        import subprocess as _sp

        monkeypatch.setattr(_sp, "run", fake_run)

        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x: captured.append(x))
        rc = handler.run(
            [
                "verify.pr_review",
                "--repo",
                "o/r",
                "--pr",
                "7",
                "--body",
                "looks good",
                "--event",
                "COMMENT",
            ]
        )

        assert rc == handler.EXIT_OK
        result = json.loads(captured[-1])
        assert result["status"] == "verified"
        assert result["event"] == "COMMENT"

    def test_unverified_side_effect_exit_nonzero(self, handler, monkeypatch) -> None:
        pre_view = json.dumps({"reviews": []})
        # submit ok, but readback always returns empty reviews → absent
        post_view = json.dumps({"reviews": []})
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if "review" in cmd and "--comment" in cmd:
                return _completed(stdout="")
            return _completed(stdout=pre_view if call_count[0] == 1 else post_view)

        import subprocess as _sp

        monkeypatch.setattr(_sp, "run", fake_run)

        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x: captured.append(x))
        rc = handler.run(
            [
                "verify.pr_review",
                "--repo",
                "o/r",
                "--pr",
                "1",
                "--body",
                "x",
            ]
        )

        assert rc != handler.EXIT_OK
        result = json.loads(captured[-1])
        assert result["status"] == "error"
        assert result["reason"] == "unverified_side_effect"
