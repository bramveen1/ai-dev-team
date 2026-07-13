"""Unit tests for router.auto_dispatch.github — the GitHub-JSON reader wrappers
on the full-auto merge path (#719).

These are the highest-consequence untested functions in auto_dispatch:
``_get_check_runs`` feeds ``_ci_green``, which gates the autonomous
``auto-merge`` label. Fixture-driven, fail-closed focused:
- ``_get_check_runs``: green / red / mixed / empty / malformed check-runs JSON.
- ``_ci_green``: must return False on anything but every required check
  reporting conclusion == 'success'.
- ``_get_open_bug_issues`` / ``_get_open_dev_prs``: list + filter behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.auto_dispatch.config import DEV_WORKER_BRANCH_PREFIX, REQUIRED_CHECKS
from router.auto_dispatch.github import (
    _ci_green,
    _get_check_runs,
    _get_open_bug_issues,
    _get_open_dev_prs,
    _TokenError,
)

pytestmark = pytest.mark.unit


def _resp(status_code, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=json_data)
    return r


def _check_run(name, conclusion, run_id=1):
    return {"id": run_id, "name": name, "conclusion": conclusion}


# ---------------------------------------------------------------------------
# _get_check_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetCheckRuns:
    async def test_single_page_returns_latest_per_name(self):
        runs = [_check_run("lint", "success"), _check_run("test-unit", "failure")]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest["lint"]["conclusion"] == "success"
        assert latest["test-unit"]["conclusion"] == "failure"

    async def test_duplicate_names_keep_highest_id(self):
        """A check that re-ran (e.g. after a re-push) leaves stale + fresh runs
        for the same name; the highest id (most recent) must win."""
        runs = [
            _check_run("lint", "failure", run_id=1),
            _check_run("lint", "success", run_id=2),
        ]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest["lint"]["conclusion"] == "success"
        assert latest["lint"]["id"] == 2

    async def test_empty_check_runs_returns_empty_dict(self):
        resp = _resp(200, {"check_runs": []})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest == {}

    async def test_malformed_json_missing_check_runs_key_returns_empty_dict(self):
        """A response body that parses but has an unexpected shape (no
        'check_runs' key) must not raise — it degrades to no known checks."""
        resp = _resp(200, {"total_count": 0})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest == {}

    async def test_paginates_when_first_page_is_full(self):
        page1 = [_check_run(f"check-{i}", "success", run_id=i) for i in range(100)]
        page2 = [_check_run("lint", "success", run_id=200)]
        resp1 = _resp(200, {"check_runs": page1})
        resp2 = _resp(200, {"check_runs": page2})
        gh = AsyncMock(side_effect=[resp1, resp2])
        with patch("router.auto_dispatch.github._gh_get", new=gh):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert len(latest) == 101
        assert gh.await_count == 2

    async def test_401_raises_token_error(self):
        resp = _resp(401)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(_TokenError):
                await _get_check_runs("o/r", "sha1", "pat")


# ---------------------------------------------------------------------------
# _ci_green — fail-closed on anything but every required check == success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCiGreen:
    async def test_all_required_checks_success_is_green(self):
        runs = [_check_run(name, "success") for name in sorted(REQUIRED_CHECKS)]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is True

    async def test_one_required_check_failed_is_red(self):
        names = sorted(REQUIRED_CHECKS)
        runs = [_check_run(n, "success") for n in names[:-1]] + [_check_run(names[-1], "failure")]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_mixed_pending_and_success_is_not_green(self):
        names = sorted(REQUIRED_CHECKS)
        runs = [_check_run(n, "success") for n in names[:-1]] + [_check_run(names[-1], None)]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_missing_required_check_is_not_green(self):
        """A required check that never ran (absent from the response) must not
        be treated as green — fail-closed on missing state."""
        names = sorted(REQUIRED_CHECKS)[:-1]  # one required check absent entirely
        runs = [_check_run(n, "success") for n in names]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_empty_check_runs_is_not_green(self):
        resp = _resp(200, {"check_runs": []})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_malformed_json_is_not_green(self):
        resp = _resp(200, {"unexpected": "shape"})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_unrelated_extra_checks_do_not_make_it_green(self):
        """Extra passing checks outside REQUIRED_CHECKS must not paper over a
        missing/failed required one."""
        runs = [_check_run("some-optional-check", "success")]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False


# ---------------------------------------------------------------------------
# _get_open_bug_issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOpenBugIssues:
    async def test_filters_out_pull_requests(self):
        items = [
            {"number": 1, "title": "real bug"},
            {"number": 2, "title": "actually a PR", "pull_request": {"url": "..."}},
        ]
        resp = _resp(200, items)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            issues = await _get_open_bug_issues("o/r", "pat")
        assert [i["number"] for i in issues] == [1]

    async def test_queries_open_bug_labelled_ascending(self):
        resp = _resp(200, [])
        gh = AsyncMock(return_value=resp)
        with patch("router.auto_dispatch.github._gh_get", new=gh):
            await _get_open_bug_issues("o/r", "pat")
        assert gh.await_args.args[0] == "/repos/o/r/issues"
        assert gh.await_args.args[1] == "pat"
        kwargs = gh.await_args.kwargs
        assert kwargs["state"] == "open"
        assert kwargs["labels"] == "bug"
        assert kwargs["sort"] == "created"
        assert kwargs["direction"] == "asc"
        assert kwargs["per_page"] == 100

    async def test_empty_list_returns_empty(self):
        resp = _resp(200, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            issues = await _get_open_bug_issues("o/r", "pat")
        assert issues == []

    async def test_401_raises_token_error(self):
        resp = _resp(401)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(_TokenError):
                await _get_open_bug_issues("o/r", "pat")


# ---------------------------------------------------------------------------
# _get_open_dev_prs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOpenDevPrs:
    async def test_filters_by_dev_worker_branch_prefix(self):
        prs = [
            {"number": 10, "head": {"ref": f"{DEV_WORKER_BRANCH_PREFIX}42-fix-bug"}},
            {"number": 11, "head": {"ref": "some-other-branch"}},
        ]
        resp = _resp(200, prs)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            dev_prs = await _get_open_dev_prs("o/r", "pat")
        assert [pr["number"] for pr in dev_prs] == [10]

    async def test_missing_head_or_ref_is_excluded_not_raised(self):
        prs = [{"number": 12, "head": {}}, {"number": 13}]
        resp = _resp(200, prs)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            dev_prs = await _get_open_dev_prs("o/r", "pat")
        assert dev_prs == []

    async def test_empty_list_returns_empty(self):
        resp = _resp(200, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            dev_prs = await _get_open_dev_prs("o/r", "pat")
        assert dev_prs == []

    async def test_401_raises_token_error(self):
        resp = _resp(401)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(_TokenError):
                await _get_open_dev_prs("o/r", "pat")
