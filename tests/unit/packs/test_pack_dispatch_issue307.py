"""Unit tests for issue #307: per-dispatch scratch trees.

Covers:

- ``_clone_repo_into_workspace``:
  - Runs the expected git commands (clone, checkout, pull) with the right URL.
  - Returns the ``<workspace>/repo/`` path.
  - Raises ``RuntimeError`` on git clone failure (non-zero exit).
  - Raises ``RuntimeError`` on git checkout failure.
  - Raises ``RuntimeError`` on git pull failure.
  - Parses owner/repo from a standard GitHub issue URL.
  - Raises ``RuntimeError`` for unparseable URLs.
- ``_seed_dispatch_identity`` with ``dispatch_repo``:
  - Includes ``DISPATCH_REPO=<path>`` in ``workspace/.env``.
  - Omits it when not provided (backward-compatible).
- ``dispatch_issue`` clone integration:
  - ``_clone_repo_fn`` is called before slot acquire (injectable seam works).
  - Clone failure returns ``{status: error, reason: clone_failed}`` and does
    not spawn a subprocess.
  - Successful clone: ``DISPATCH_REPO`` appears in ``workspace/.env``.
  - Prompt includes ``$DISPATCH_REPO`` instruction and forbids other scratch paths.
  - Clone is skipped when ``exec_override`` is set.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_issue307_handler",
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


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


# D-7: approval gate is fail-closed. Tests not covering gating bypass it.
_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 77000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


# ── _clone_repo_into_workspace ────────────────────────────────────────────────


class TestCloneRepoIntoWorkspace:
    def _make_run(self, *completeds: MagicMock) -> Any:
        """Return a fake ``run`` that returns completeds in order."""
        calls = list(completeds)

        def fake_run(cmd, **kwargs):
            return calls.pop(0)

        return fake_run

    def test_runs_three_git_commands(self, handler, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def recording_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/bramveen1/ai-dev-team/issues/307",
            run=recording_run,
        )

        assert len(calls) == 3
        assert calls[0][:3] == ["git", "clone", "--depth=50"]
        assert calls[1][0] == "git"
        assert calls[1][1] == "-C"
        assert calls[1][2] == str(workspace / "repo")
        assert calls[1][3] == "checkout"
        assert calls[2][0] == "git"
        assert calls[2][1] == "-C"
        assert calls[2][3] == "pull"

    def test_clone_url_built_from_issue_url(self, handler, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def recording_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/myorg/myrepo/issues/42",
            run=recording_run,
        )

        clone_cmd = calls[0]
        assert "https://github.com/myorg/myrepo.git" in clone_cmd
        assert str(workspace / "repo") in clone_cmd

    def test_returns_workspace_repo_path(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        result = handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/bramveen1/ai-dev-team/issues/1",
            run=lambda cmd, **kw: _completed(),
        )
        assert result == workspace / "repo"

    def test_raises_on_clone_failure(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError, match="git clone failed"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/1",
                run=lambda cmd, **kw: _completed(returncode=128, stderr="repository not found"),
            )

    def test_raises_on_checkout_failure(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        call_count = 0

        def run_with_checkout_fail(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return _completed(returncode=1, stderr="pathspec 'main' did not match")
            return _completed()

        with pytest.raises(RuntimeError, match="git -C failed"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/1",
                run=run_with_checkout_fail,
            )

    def test_raises_on_pull_failure(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        call_count = 0

        def run_with_pull_fail(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                return _completed(returncode=1, stderr="not possible to fast-forward")
            return _completed()

        with pytest.raises(RuntimeError, match="git -C failed"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/1",
                run=run_with_pull_fail,
            )

    def test_raises_for_non_github_url(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError, match="cannot parse owner/repo"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://gitlab.com/owner/repo/issues/1",
                run=lambda cmd, **kw: _completed(),
            )

    def test_raises_for_malformed_github_url(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError, match="cannot parse owner/repo"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/onlyowner",
                run=lambda cmd, **kw: _completed(),
            )

    def test_depth_50_in_clone_command(self, handler, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def recording_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/bramveen1/ai-dev-team/issues/307",
            run=recording_run,
        )
        assert "--depth=50" in calls[0]


# ── _seed_dispatch_identity with dispatch_repo ───────────────────────────────


class TestSeedDispatchIdentityWithRepo:
    def test_dispatch_repo_written_to_env(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo_path = workspace / "repo"
        handler._seed_dispatch_identity(workspace, "ghp_tok", dispatch_repo=repo_path)
        content = (workspace / ".env").read_text()
        assert f"DISPATCH_REPO={repo_path}" in content

    def test_dispatch_repo_absent_when_not_provided(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._seed_dispatch_identity(workspace, "ghp_tok")
        content = (workspace / ".env").read_text()
        assert "DISPATCH_REPO" not in content

    def test_tokens_still_written_with_dispatch_repo(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._seed_dispatch_identity(workspace, "ghp_mytoken", dispatch_repo=workspace / "repo")
        content = (workspace / ".env").read_text()
        assert "GH_TOKEN=ghp_mytoken" in content
        assert "GITHUB_TOKEN=ghp_mytoken" in content

    def test_env_file_mode_0600(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._seed_dispatch_identity(workspace, "ghp_tok", dispatch_repo=workspace / "repo")
        assert oct((workspace / ".env").stat().st_mode & 0o777) == oct(0o600)


# ── dispatch_issue clone integration ─────────────────────────────────────────


class TestDispatchIssueCloneIntegration:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def _base_kwargs(self, tmp_path: Path, token_file: Path | None = None) -> dict:
        return dict(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/307",
            channel="",
            thread_ts="",
            agent="sam",
            workspace_root=tmp_path,
            _seed_auth_fn=_no_op_seed_auth,
            _slack_token=None,
            _approval_cfg=_NO_GATE_CFG,
            popen=_FakePopen,
            supervision_mode="inline",
            _dispatch_token_path=token_file or (tmp_path / "no.token"),
        )

    def test_clone_fn_called_before_dispatch(self, handler, tmp_path: Path) -> None:
        """_clone_repo_fn must be called (injectable seam works)."""
        clone_calls: list[tuple] = []

        def recording_clone(workspace, issue_url):
            clone_calls.append((workspace, issue_url))
            return workspace / "repo"

        handler.dispatch_issue(
            **self._base_kwargs(tmp_path),
            _clone_repo_fn=recording_clone,
        )
        assert len(clone_calls) == 1
        _, called_url = clone_calls[0]
        assert called_url == "https://github.com/bramveen1/ai-dev-team/issues/307"

    def test_clone_failure_returns_error_before_spawning(self, handler, tmp_path: Path) -> None:
        """Clone failure must return structured error without spawning a babysit."""

        def failing_clone(workspace, issue_url):
            raise RuntimeError("git clone failed: authentication failed")

        result = handler.dispatch_issue(
            **self._base_kwargs(tmp_path),
            _clone_repo_fn=failing_clone,
        )

        assert result["status"] == "error"
        assert result["reason"] == "clone_failed"
        assert "authentication failed" in result["detail"]
        # No babysit spawned.
        assert _FakePopen.instances == []

    def test_clone_failure_before_slot_acquire(self, handler, tmp_path: Path) -> None:
        """Slot acquire must not happen when clone fails (no slot file created)."""

        def failing_clone(workspace, issue_url):
            raise RuntimeError("git clone failed: network error")

        result = handler.dispatch_issue(
            **self._base_kwargs(tmp_path),
            _clone_repo_fn=failing_clone,
        )
        assert result["status"] == "error"
        assert result["reason"] == "clone_failed"
        # No slot files should exist.
        slots_dir = tmp_path / ".slots"
        if slots_dir.exists():
            slot_files = list(slots_dir.iterdir())
            assert slot_files == [], f"slot files created: {slot_files}"

    def test_dispatch_repo_in_env_when_token_available(self, handler, tmp_path: Path) -> None:
        """DISPATCH_REPO must appear in workspace/.env when dispatch token is present."""
        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_dispatchtoken\n")

        def stub_clone(workspace, issue_url):
            return workspace / "repo"

        handler.dispatch_issue(
            **self._base_kwargs(tmp_path, token_file),
            _clone_repo_fn=stub_clone,
        )

        # Find the dispatch workspace that was created.
        dispatch_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("dispatch-")]
        assert dispatch_dirs, "no dispatch workspace created"
        workspace = dispatch_dirs[0]
        env_content = (workspace / ".env").read_text()
        assert "DISPATCH_REPO=" in env_content
        assert str(workspace / "repo") in env_content

    def test_clone_skipped_when_exec_override_set(self, handler, tmp_path: Path) -> None:
        """exec_override mode must not call _clone_repo_fn."""
        clone_called = []

        def should_not_be_called(workspace, issue_url):
            clone_called.append(True)
            return workspace / "repo"

        handler.dispatch_issue(
            **self._base_kwargs(tmp_path),
            exec_override=["sleep", "0"],
            _clone_repo_fn=should_not_be_called,
        )
        assert clone_called == [], "clone must be skipped when exec_override is set"


# ── Worker prompt includes DISPATCH_REPO instruction ─────────────────────────


class TestBuildClaudeCommandPrompt:
    def test_prompt_mentions_dispatch_repo(self, handler, tmp_path: Path) -> None:
        cmd = handler._build_claude_command(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/307",
            model="sonnet",
            persona="dev",
            workspace=tmp_path,
        )
        # Find the prompt string (second element after "claude -p")
        prompt = cmd[cmd.index("-p") + 1]
        assert "$DISPATCH_REPO" in prompt

    def test_prompt_forbids_other_scratch_paths(self, handler, tmp_path: Path) -> None:
        cmd = handler._build_claude_command(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/307",
            model="sonnet",
            persona="dev",
            workspace=tmp_path,
        )
        prompt = cmd[cmd.index("-p") + 1]
        # Must forbid creating other scratch paths.
        assert "sam-scratch" in prompt or "other scratch" in prompt.lower()


# ── Entrypoint cleanup ────────────────────────────────────────────────────────


class TestEntrypointCleanup:
    def test_entrypoint_removes_sam_scratch(self) -> None:
        """docker/entrypoint.sh must contain idempotent rm -rf /tmp/sam-scratch."""
        entrypoint = REPO_ROOT / "docker" / "entrypoint.sh"
        assert entrypoint.exists(), "docker/entrypoint.sh must exist"
        content = entrypoint.read_text()
        assert "rm -rf /tmp/sam-scratch" in content, (
            "entrypoint.sh must contain 'rm -rf /tmp/sam-scratch' (issue #307 AC5)"
        )
