"""Unit tests for router.auto_dispatch.github — GitHub-JSON reader wrappers
on the full-auto merge path (#719).

Fixture-driven coverage for the three previously-untested JSON parsers:
``_get_check_runs`` (feeds the auto-merge CI gate via ``_ci_green``),
``_get_open_bug_issues``, and ``_get_open_dev_prs``. ``_ci_green`` is
asserted fail-closed: only an exact match of all ``REQUIRED_CHECKS`` with
conclusion "success" yields True.
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

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _resp(status_code: int, json_data):
    """A minimal stand-in for an httpx.Response used by _gh_get callers."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = RuntimeError(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def _check_run(name: str, conclusion: str | None, run_id: int = 1) -> dict:
    return {"id": run_id, "name": name, "conclusion": conclusion, "status": "completed"}


def _all_green_runs() -> list[dict]:
    return [_check_run(name, "success", run_id=i) for i, name in enumerate(sorted(REQUIRED_CHECKS), start=1)]


# ---------------------------------------------------------------------------
# _get_check_runs / _ci_green — the auto-merge CI gate
# ---------------------------------------------------------------------------


class TestGetCheckRuns:
    async def test_returns_latest_run_per_name(self):
        resp = _resp(200, {"check_runs": _all_green_runs()})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert set(latest) == REQUIRED_CHECKS
        assert all(run["conclusion"] == "success" for run in latest.values())

    async def test_keeps_highest_id_when_name_duplicated(self):
        runs = [_check_run("lint", "failure", run_id=1), _check_run("lint", "success", run_id=2)]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest["lint"]["conclusion"] == "success"
        assert latest["lint"]["id"] == 2

    async def test_paginates_full_pages(self):
        page1 = [_check_run(f"c{i}", "success", run_id=i) for i in range(100)]
        page2 = [_check_run("lint", "success", run_id=200)]
        resp1 = _resp(200, {"check_runs": page1})
        resp2 = _resp(200, {"check_runs": page2})
        gh = AsyncMock(side_effect=[resp1, resp2])
        with patch("router.auto_dispatch.github._gh_get", new=gh):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert len(latest) == 101
        assert gh.await_count == 2

    async def test_empty_check_runs_list_yields_empty_dict(self):
        resp = _resp(200, {"check_runs": []})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest == {}

    async def test_missing_check_runs_key_yields_empty_dict(self):
        """Malformed body (no 'check_runs' key at all) must not crash the parser."""
        resp = _resp(200, {})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            latest = await _get_check_runs("o/r", "sha1", "pat")
        assert latest == {}

    async def test_401_raises_token_error(self):
        resp = _resp(401, {})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(_TokenError):
                await _get_check_runs("o/r", "sha1", "pat")

    async def test_non_200_raises(self):
        resp = _resp(500, {})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(RuntimeError):
                await _get_check_runs("o/r", "sha1", "pat")


class TestCiGreenFailClosed:
    """``_ci_green`` gates auto-merge — every non-success path must be False."""

    async def test_all_required_checks_success_is_green(self):
        resp = _resp(200, {"check_runs": _all_green_runs()})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is True

    async def test_one_failure_is_not_green(self):
        runs = _all_green_runs()
        runs[0]["conclusion"] = "failure"
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_pending_check_conclusion_none_is_not_green(self):
        runs = _all_green_runs()
        runs[0]["conclusion"] = None
        runs[0]["status"] = "in_progress"
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_missing_required_check_is_not_green(self):
        runs = [r for r in _all_green_runs() if r["name"] != "compose-check"]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_empty_check_runs_is_not_green(self):
        resp = _resp(200, {"check_runs": []})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_malformed_body_is_not_green(self):
        """No 'check_runs' key at all — must fail closed, not raise past the gate."""
        resp = _resp(200, {"unexpected": "shape"})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False

    async def test_unrelated_check_names_do_not_satisfy_gate(self):
        """Extra/irrelevant check names present, but none of REQUIRED_CHECKS — not green."""
        runs = [_check_run("codeql", "success", run_id=1), _check_run("codeql-2", "success", run_id=2)]
        resp = _resp(200, {"check_runs": runs})
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _ci_green("o/r", "sha1", "pat") is False


# ---------------------------------------------------------------------------
# _get_open_bug_issues
# ---------------------------------------------------------------------------


class TestGetOpenBugIssues:
    async def test_filters_out_pull_requests(self):
        payload = [
            {"number": 1, "title": "real issue"},
            {"number": 2, "title": "actually a PR", "pull_request": {"url": "..."}},
        ]
        resp = _resp(200, payload)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            issues = await _get_open_bug_issues("o/r", "pat")
        assert [i["number"] for i in issues] == [1]

    async def test_requests_open_bug_labeled_issues_sorted_ascending(self):
        resp = _resp(200, [])
        gh = AsyncMock(return_value=resp)
        with patch("router.auto_dispatch.github._gh_get", new=gh):
            await _get_open_bug_issues("o/r", "pat")
        gh.assert_awaited_once_with(
            "/repos/o/r/issues",
            "pat",
            state="open",
            labels="bug",
            sort="created",
            direction="asc",
            per_page=100,
        )

    async def test_empty_result_yields_empty_list(self):
        resp = _resp(200, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _get_open_bug_issues("o/r", "pat") == []

    async def test_401_raises_token_error(self):
        resp = _resp(401, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(_TokenError):
                await _get_open_bug_issues("o/r", "pat")

    async def test_non_200_raises(self):
        resp = _resp(503, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(RuntimeError):
                await _get_open_bug_issues("o/r", "pat")


# ---------------------------------------------------------------------------
# _get_open_dev_prs
# ---------------------------------------------------------------------------


class TestGetOpenDevPrs:
    async def test_filters_to_dev_worker_branch_prefix(self):
        payload = [
            {"number": 10, "head": {"ref": f"{DEV_WORKER_BRANCH_PREFIX}42-fix-bug"}},
            {"number": 11, "head": {"ref": "unrelated-branch"}},
        ]
        resp = _resp(200, payload)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            prs = await _get_open_dev_prs("o/r", "pat")
        assert [pr["number"] for pr in prs] == [10]

    async def test_missing_head_field_does_not_crash(self):
        """Malformed PR entry with no 'head' key must be filtered out, not raise."""
        payload = [{"number": 12}]
        resp = _resp(200, payload)
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            prs = await _get_open_dev_prs("o/r", "pat")
        assert prs == []

    async def test_empty_result_yields_empty_list(self):
        resp = _resp(200, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            assert await _get_open_dev_prs("o/r", "pat") == []

    async def test_401_raises_token_error(self):
        resp = _resp(401, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(_TokenError):
                await _get_open_dev_prs("o/r", "pat")

    async def test_non_200_raises(self):
        resp = _resp(500, [])
        with patch("router.auto_dispatch.github._gh_get", new=AsyncMock(return_value=resp)):
            with pytest.raises(RuntimeError):
                await _get_open_dev_prs("o/r", "pat")
