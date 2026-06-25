"""Regression tests for issue #549: existing-PR dispatch mode.

Covers:
- _resolve_pr_head_branch: happy path, gh failure, timeout, missing binary, empty headRefName
- _build_claude_command: PR-mode prompt mentions --force-with-lease and head branch,
  standard mode unchanged
- dispatch_issue with pr_url: resolves head branch, writes sidecar files (pr_url,
  head_branch), injects DISPATCH_HEAD_BRANCH env, includes pr_url/head_branch in response
- dispatch_issue with pr_url: pr_resolve_failed error path surfaces structured error
- dispatch_issue standard (issue_url only): unchanged behaviour — no head_branch written,
  no DISPATCH_HEAD_BRANCH injected, standard prompt used
- _resolve_pr_head_branch injectable seam used when _resolve_pr_head_branch_fn is provided
- CLI --pr-url flag: run() forwards pr_url to dispatch_issue
- CLI: missing both --issue-url and --pr-url exits nonzero
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


# ── Helpers shared with the main test module ─────────────────────────


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


def _no_op_clone(workspace: Path, repo_url: str) -> Path:
    return workspace / "repo"


def _no_op_clone_pr(workspace: Path, repo_url: str, *, head_branch: str | None = None) -> Path:
    """Clone stub that accepts the head_branch kwarg (existing-PR mode)."""
    return workspace / "repo"


_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_handler_549",
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


class _FakePopen:
    instances: list["_FakePopen"] = []
    wait_returncode = 0
    wait_writes_exitcode = True

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 99999
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.wait_writes_exitcode and self.cwd:
            (Path(self.cwd) / "exitcode").write_text(str(self.wait_returncode))
        return self.wait_returncode

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.wait_returncode = 0
        cls.wait_writes_exitcode = True


# ── _resolve_pr_head_branch ───────────────────────────────────────────


class TestResolvePrHeadBranch:
    def test_happy_path_returns_head_ref_and_repo(self, handler) -> None:
        payload = json.dumps(
            {
                "headRefName": "issue-549-my-feature",
                "headRepository": {"nameWithOwner": "bramveen1/ai-dev-team"},
            }
        )

        def fake_run(argv, **kw):
            return _completed(stdout=payload)

        head_ref, head_repo = handler._resolve_pr_head_branch(
            "https://github.com/bramveen1/ai-dev-team/pull/549",
            run=fake_run,
        )
        assert head_ref == "issue-549-my-feature"
        assert head_repo == "bramveen1/ai-dev-team"

    def test_nonzero_exit_raises_runtime_error(self, handler) -> None:
        def fake_run(argv, **kw):
            return _completed(stderr="not found", returncode=1)

        with pytest.raises(RuntimeError, match="gh pr view failed"):
            handler._resolve_pr_head_branch(
                "https://github.com/bramveen1/ai-dev-team/pull/999",
                run=fake_run,
            )

    def test_timeout_raises_runtime_error(self, handler) -> None:
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=15)

        with pytest.raises(RuntimeError, match="timed out"):
            handler._resolve_pr_head_branch(
                "https://github.com/bramveen1/ai-dev-team/pull/1",
                run=boom,
            )

    def test_missing_binary_raises_runtime_error(self, handler) -> None:
        def boom(argv, **kw):
            raise FileNotFoundError("gh not found")

        with pytest.raises(RuntimeError, match="gh binary not found"):
            handler._resolve_pr_head_branch(
                "https://github.com/bramveen1/ai-dev-team/pull/1",
                run=boom,
            )

    def test_invalid_json_raises_runtime_error(self, handler) -> None:
        def fake_run(argv, **kw):
            return _completed(stdout="not-json")

        with pytest.raises(RuntimeError, match="invalid JSON"):
            handler._resolve_pr_head_branch(
                "https://github.com/bramveen1/ai-dev-team/pull/1",
                run=fake_run,
            )

    def test_missing_head_ref_raises_runtime_error(self, handler) -> None:
        payload = json.dumps({"headRefName": "", "headRepository": {"nameWithOwner": "o/r"}})

        def fake_run(argv, **kw):
            return _completed(stdout=payload)

        with pytest.raises(RuntimeError, match="no headRefName"):
            handler._resolve_pr_head_branch(
                "https://github.com/bramveen1/ai-dev-team/pull/1",
                run=fake_run,
            )

    def test_passes_pr_url_to_gh_cli(self, handler) -> None:
        seen: list[list[str]] = []

        def recording_run(argv, **kw):
            seen.append(list(argv))
            return _completed(
                stdout=json.dumps(
                    {
                        "headRefName": "my-branch",
                        "headRepository": {"nameWithOwner": "o/r"},
                    }
                )
            )

        pr_url = "https://github.com/bramveen1/ai-dev-team/pull/549"
        handler._resolve_pr_head_branch(pr_url, run=recording_run)
        assert len(seen) == 1
        assert pr_url in seen[0]
        assert "--json" in seen[0]


# ── _build_claude_command: PR mode vs standard mode ───────────────────


class TestBuildClaudeCommandPrMode:
    def test_pr_mode_prompt_mentions_force_with_lease(self, handler, tmp_path: Path) -> None:
        cmd = handler._build_claude_command(
            issue_url="",
            model="sonnet",
            persona="dev",
            workspace=tmp_path,
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            head_branch="issue-549-my-feature",
        )
        prompt = cmd[cmd.index("-p") + 1]
        assert "--force-with-lease" in prompt
        assert "issue-549-my-feature" in prompt
        assert "do NOT" in prompt or "do not" in prompt.lower()

    def test_pr_mode_prompt_does_not_say_open_a_new_pr(self, handler, tmp_path: Path) -> None:
        cmd = handler._build_claude_command(
            issue_url="",
            model="sonnet",
            persona="dev",
            workspace=tmp_path,
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            head_branch="issue-549-my-feature",
        )
        prompt = cmd[cmd.index("-p") + 1]
        assert "do NOT open a new PR" in prompt or "do not open a new PR" in prompt.lower()
        # The PR-mode prompt must NOT also carry the standard "fresh branch /
        # open a pull request" opener — that contradiction is exactly the
        # failure #549 exists to kill. Guard both directives' absence.
        assert "open a pull request" not in prompt.lower()
        assert "fresh branch" not in prompt.lower()

    def test_standard_mode_prompt_mentions_issue_url(self, handler, tmp_path: Path) -> None:
        issue_url = "https://github.com/bramveen1/ai-dev-team/issues/549"
        cmd = handler._build_claude_command(
            issue_url=issue_url,
            model="sonnet",
            persona="dev",
            workspace=tmp_path,
        )
        prompt = cmd[cmd.index("-p") + 1]
        assert issue_url in prompt
        assert "--force-with-lease" not in prompt

    def test_pr_mode_only_when_both_pr_url_and_head_branch_set(self, handler, tmp_path: Path) -> None:
        """pr_url alone (no head_branch) must fall back to standard prompt."""
        issue_url = "https://github.com/bramveen1/ai-dev-team/issues/549"
        cmd = handler._build_claude_command(
            issue_url=issue_url,
            model="sonnet",
            persona="dev",
            workspace=tmp_path,
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            head_branch=None,
        )
        prompt = cmd[cmd.index("-p") + 1]
        assert "--force-with-lease" not in prompt
        assert issue_url in prompt


# ── dispatch_issue: existing-PR mode (pr_url provided) ───────────────


class TestDispatchIssueExistingPrMode:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def _fake_resolve(self, pr_url: str) -> tuple[str, str]:
        return "issue-549-existing-pr-dispatch-mode", "bramveen1/ai-dev-team"

    def test_writes_pr_url_and_head_branch_sidecar_files(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            budget_seconds=600,
            model="sonnet",
            persona="dev",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone_pr,
            _approval_cfg=_NO_GATE_CFG,
            _resolve_pr_head_branch_fn=self._fake_resolve,
        )
        assert result["status"] == "launched"
        workspace = Path(result["workspace"])
        assert (workspace / "pr_url").read_text() == "https://github.com/bramveen1/ai-dev-team/pull/549"
        assert (workspace / "head_branch").read_text() == "issue-549-existing-pr-dispatch-mode"

    def test_response_includes_pr_url_and_head_branch(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone_pr,
            _approval_cfg=_NO_GATE_CFG,
            _resolve_pr_head_branch_fn=self._fake_resolve,
        )
        assert result["status"] == "launched"
        assert result["pr_url"] == "https://github.com/bramveen1/ai-dev-team/pull/549"
        assert result["head_branch"] == "issue-549-existing-pr-dispatch-mode"

    def test_injects_dispatch_head_branch_env_into_worker(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone_pr,
            _approval_cfg=_NO_GATE_CFG,
            _resolve_pr_head_branch_fn=self._fake_resolve,
        )
        assert result["status"] == "launched"
        popen = _FakePopen.instances[0]
        env = popen.kwargs.get("env") or {}
        assert handler.DISPATCH_HEAD_BRANCH_ENV in env
        assert env[handler.DISPATCH_HEAD_BRANCH_ENV] == "issue-549-existing-pr-dispatch-mode"

    def test_pr_resolve_failure_returns_structured_error(self, handler, tmp_path: Path) -> None:
        def boom(pr_url: str) -> tuple[str, str]:
            raise RuntimeError("gh pr view failed (exit 1): not found")

        result = handler.dispatch_issue(
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/9999",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone_pr,
            _approval_cfg=_NO_GATE_CFG,
            _resolve_pr_head_branch_fn=boom,
        )
        assert result["status"] == "error"
        assert result["reason"] == "pr_resolve_failed"
        assert "detail" in result
        workspace = Path(result["workspace"])
        assert (workspace / "error_reason").read_text() == "pr_resolve_failed"

    def test_uses_injectable_resolve_fn(self, handler, tmp_path: Path) -> None:
        """_resolve_pr_head_branch_fn is called with the pr_url, not the real gh CLI."""
        called_with: list[str] = []

        def recording_resolve(pr_url: str) -> tuple[str, str]:
            called_with.append(pr_url)
            return "my-branch", "o/r"

        handler.dispatch_issue(
            pr_url="https://github.com/bramveen1/ai-dev-team/pull/549",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone_pr,
            _approval_cfg=_NO_GATE_CFG,
            _resolve_pr_head_branch_fn=recording_resolve,
        )
        assert called_with == ["https://github.com/bramveen1/ai-dev-team/pull/549"]


# ── dispatch_issue: standard (issue_url) mode unchanged ──────────────


class TestDispatchIssueStandardModeUnchanged:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_no_head_branch_sidecar_written(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert result["status"] == "launched"
        workspace = Path(result["workspace"])
        assert not (workspace / "head_branch").exists()
        assert not (workspace / "pr_url").exists()

    def test_no_head_branch_in_response(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert "head_branch" not in result
        assert "pr_url" not in result

    def test_dispatch_head_branch_env_not_injected(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone,
            _approval_cfg=_NO_GATE_CFG,
        )
        assert result["status"] == "launched"
        popen = _FakePopen.instances[0]
        env = popen.kwargs.get("env") or {}
        assert handler.DISPATCH_HEAD_BRANCH_ENV not in env

    def test_resolve_fn_not_called_in_standard_mode(self, handler, tmp_path: Path) -> None:
        called: list[bool] = []

        def should_not_be_called(pr_url: str) -> tuple[str, str]:
            called.append(True)
            return "branch", "o/r"

        handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C123",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone,
            _approval_cfg=_NO_GATE_CFG,
            _resolve_pr_head_branch_fn=should_not_be_called,
        )
        assert called == [], "resolve fn must not be called in standard issue mode"


# ── CLI: --pr-url flag wired through run() ────────────────────────────


class TestCliPrUrl:
    def test_pr_url_forwarded_to_dispatch_issue(self, handler, tmp_path: Path, capsys, monkeypatch) -> None:
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "disp-test", "workspace": str(tmp_path), "pid": 1}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)

        rc = handler.run(
            [
                "dispatch_issue",
                "--pr-url",
                "https://github.com/bramveen1/ai-dev-team/pull/549",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--agent",
                "sam",
            ]
        )
        assert rc == 0
        assert recorded["pr_url"] == "https://github.com/bramveen1/ai-dev-team/pull/549"
        assert recorded.get("issue_url", "") == "" or recorded.get("issue_url") is None

    def test_missing_both_issue_url_and_pr_url_exits_nonzero(self, handler, capsys, monkeypatch) -> None:
        monkeypatch.setattr(handler, "dispatch_issue", lambda **kw: {})

        rc = handler.run(
            [
                "dispatch_issue",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--agent",
                "sam",
            ]
        )
        assert rc != 0

    def test_issue_url_alone_still_works(self, handler, tmp_path: Path, capsys, monkeypatch) -> None:
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "disp-test", "workspace": str(tmp_path), "pid": 1}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/bramveen1/ai-dev-team/issues/42",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--agent",
                "sam",
            ]
        )
        assert rc == 0
        assert recorded["issue_url"] == "https://github.com/bramveen1/ai-dev-team/issues/42"
        assert recorded.get("pr_url") is None
