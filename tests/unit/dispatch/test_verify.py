"""Unit tests for packs.dispatch.verify — verify_issue_create.

All _run calls are faked; no live network or gh binary required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from packs.dispatch.verify import (
    GhApiError,
    UnverifiedSideEffect,
    verify_issue_create,
)

pytestmark = pytest.mark.unit

REPO = "bramveen1/ai-dev-team"
TITLE = "Test issue"
BODY = "Test body"
ISSUE_NUMBER = 42
ISSUE_URL = f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}"


def _make_run(*responses: tuple[int, str, str]):
    """Return a fake subprocess.run replacement that returns successive responses."""
    calls = list(responses)

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        rc, stdout, stderr = calls.pop(0)
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    return fake_run


def _list_stdout(numbers: list[int], repo: str = REPO) -> str:
    return json.dumps([{"number": n, "url": f"https://github.com/{repo}/issues/{n}"} for n in numbers])


class TestVerifyIssueCreateSuccess:
    def test_returns_verified_status(self):
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),  # gh issue create → URL in stdout
            (0, _list_stdout([ISSUE_NUMBER]), ""),  # gh issue list readback
        )
        result = verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)
        assert result["status"] == "verified"

    def test_returns_correct_number_and_url(self):
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),
            (0, _list_stdout([ISSUE_NUMBER]), ""),
        )
        result = verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)
        assert result["number"] == ISSUE_NUMBER
        assert result["url"] == ISSUE_URL

    def test_receipts_included(self):
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),
            (0, _list_stdout([ISSUE_NUMBER]), ""),
        )
        result = verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)
        receipts = result["receipts"]
        assert "create_stdout" in receipts
        assert "create_exit" in receipts
        assert "list_stdout" in receipts
        assert "list_exit" in receipts

    def test_url_parsed_from_stdout_with_surrounding_text(self):
        """gh may print extra lines; URL must be found anywhere in stdout."""
        noisy_stdout = f"Creating issue in {REPO}...\n{ISSUE_URL}\nDone.\n"
        fake_run = _make_run(
            (0, noisy_stdout, ""),
            (0, _list_stdout([ISSUE_NUMBER]), ""),
        )
        result = verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)
        assert result["number"] == ISSUE_NUMBER
        assert result["url"] == ISSUE_URL

    def test_labels_do_not_break_success(self):
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),
            (0, _list_stdout([ISSUE_NUMBER]), ""),
        )
        result = verify_issue_create(REPO, TITLE, BODY, labels=["bug"], run=fake_run, _sleep_fn=lambda s: None)
        assert result["status"] == "verified"

    def test_readback_finds_issue_on_second_attempt(self):
        sleep_calls: list[float] = []
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),
            (0, _list_stdout([]), ""),  # first list: issue not yet visible
            (0, _list_stdout([ISSUE_NUMBER]), ""),  # second list: appears
        )
        result = verify_issue_create(
            REPO, TITLE, BODY, run=fake_run, max_retries=3, backoff_s=0.0, _sleep_fn=lambda s: sleep_calls.append(s)
        )
        assert result["status"] == "verified"
        assert len(sleep_calls) == 1  # slept before second attempt


class TestVerifyIssueCreateFailure:
    def test_create_failure_raises_gh_api_error(self):
        fake_run = _make_run(
            (1, "", "HTTP 422 Unprocessable Entity"),
        )
        with pytest.raises(GhApiError):
            verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)

    def test_unparseable_stdout_raises_unverified(self):
        """No URL in stdout → UnverifiedSideEffect, not a crash."""
        fake_run = _make_run(
            (0, "Something went wrong but exit was 0\n", ""),
        )
        with pytest.raises(UnverifiedSideEffect):
            verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)

    def test_list_not_finding_issue_after_retries_raises_unverified(self):
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),
            (0, _list_stdout([99, 100]), ""),  # different issues, not ours
            (0, _list_stdout([99, 100]), ""),
            (0, _list_stdout([99, 100]), ""),
        )
        with pytest.raises(UnverifiedSideEffect):
            verify_issue_create(REPO, TITLE, BODY, run=fake_run, max_retries=3, backoff_s=0.0, _sleep_fn=lambda s: None)

    def test_list_http_error_raises_gh_api_error(self):
        fake_run = _make_run(
            (0, ISSUE_URL + "\n", ""),
            (1, "", "HTTP 403 Forbidden"),
        )
        with pytest.raises(GhApiError):
            verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)

    def test_create_with_unknown_flag_json_would_fail(self):
        """Regression: the old --json flag caused gh to exit non-zero; ensure we no longer hit that."""
        # Simulate what gh used to return with unknown flag
        fake_run = _make_run(
            (1, "", "unknown flag: --json"),
        )
        with pytest.raises(GhApiError):
            verify_issue_create(REPO, TITLE, BODY, run=fake_run, _sleep_fn=lambda s: None)
