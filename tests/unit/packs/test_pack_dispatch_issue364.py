"""Unit tests for issue #364: dispatch pack hardening.

Covers:
- Part A (#348): _clone_repo_into_workspace passes explicit timeouts to git
  subprocess calls and converts TimeoutExpired to RuntimeError.
- Part B (#350): dispatch_cancel preserves the workspace directory when the
  terminal state-file write fails (OSError), and surfaces the failure via
  state_write_failed in the return value.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_issue364_handler",
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


# ── Part A: git-clone timeout (#348) ─────────────────────────────────────────


class TestCloneTimeoutConstants:
    """Timeout constants exist and have sensible values."""

    def test_git_clone_timeout_constant_exists(self, handler) -> None:
        assert hasattr(handler, "GIT_CLONE_TIMEOUT_S")
        assert isinstance(handler.GIT_CLONE_TIMEOUT_S, (int, float))
        assert handler.GIT_CLONE_TIMEOUT_S > 0

    def test_git_checkout_timeout_constant_exists(self, handler) -> None:
        assert hasattr(handler, "GIT_CHECKOUT_TIMEOUT_S")
        assert isinstance(handler.GIT_CHECKOUT_TIMEOUT_S, (int, float))
        assert handler.GIT_CHECKOUT_TIMEOUT_S > 0

    def test_git_pull_timeout_constant_exists(self, handler) -> None:
        assert hasattr(handler, "GIT_PULL_TIMEOUT_S")
        assert isinstance(handler.GIT_PULL_TIMEOUT_S, (int, float))
        assert handler.GIT_PULL_TIMEOUT_S > 0

    def test_clone_timeout_is_longest(self, handler) -> None:
        """Clone (network transfer) must budget more time than checkout/pull."""
        assert handler.GIT_CLONE_TIMEOUT_S >= handler.GIT_CHECKOUT_TIMEOUT_S
        assert handler.GIT_CLONE_TIMEOUT_S >= handler.GIT_PULL_TIMEOUT_S


class TestClonePassesTimeouts:
    """subprocess.run receives explicit timeout= for each git command."""

    def test_clone_receives_timeout_kwarg(self, handler, tmp_path: Path) -> None:
        seen_kwargs: list[dict] = []

        def recording_run(cmd, **kwargs):
            seen_kwargs.append(dict(kwargs))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/bramveen1/ai-dev-team/issues/364",
            run=recording_run,
        )

        assert len(seen_kwargs) == 3, "expected 3 git calls"
        for i, kw in enumerate(seen_kwargs):
            assert "timeout" in kw, f"git call {i} missing timeout kwarg: {kw!r}"
            assert kw["timeout"] is not None and kw["timeout"] > 0

    def test_each_git_command_has_distinct_timeout(self, handler, tmp_path: Path) -> None:
        """Clone, checkout, and pull may each have different timeout values."""
        recorded: list[tuple[list[str], dict]] = []

        def recording_run(cmd, **kwargs):
            recorded.append((list(cmd), dict(kwargs)))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/bramveen1/ai-dev-team/issues/364",
            run=recording_run,
        )

        # All timeouts must be positive.
        for cmd, kwargs in recorded:
            assert kwargs.get("timeout", 0) > 0, f"{cmd[1]} call missing positive timeout"

        # Clone (first call) must use GIT_CLONE_TIMEOUT_S.
        _, clone_kwargs = recorded[0]
        assert clone_kwargs["timeout"] == handler.GIT_CLONE_TIMEOUT_S

        # Checkout (second call) must use GIT_CHECKOUT_TIMEOUT_S.
        _, checkout_kwargs = recorded[1]
        assert checkout_kwargs["timeout"] == handler.GIT_CHECKOUT_TIMEOUT_S

        # Pull (third call) must use GIT_PULL_TIMEOUT_S.
        _, pull_kwargs = recorded[2]
        assert pull_kwargs["timeout"] == handler.GIT_PULL_TIMEOUT_S


class TestCloneTimeoutExpiredConverted:
    """TimeoutExpired on any git op is converted to RuntimeError (issue #348)."""

    def test_clone_timeout_raises_runtime_error(self, handler, tmp_path: Path) -> None:
        def run_that_times_out(cmd, **kwargs):
            if cmd[1] == "clone":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError, match="timed out"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/364",
                run=run_that_times_out,
            )

    def test_checkout_timeout_raises_runtime_error(self, handler, tmp_path: Path) -> None:
        call_count = [0]

        def run_with_checkout_timeout(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError, match="timed out"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/364",
                run=run_with_checkout_timeout,
            )

    def test_pull_timeout_raises_runtime_error(self, handler, tmp_path: Path) -> None:
        call_count = [0]

        def run_with_pull_timeout(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
            return _completed()

        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError, match="timed out"):
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/364",
                run=run_with_pull_timeout,
            )

    def test_timeout_error_message_includes_operation_name(self, handler, tmp_path: Path) -> None:
        """RuntimeError message should identify which git operation timed out."""

        def run_that_times_out(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RuntimeError) as exc_info:
            handler._clone_repo_into_workspace(
                workspace,
                "https://github.com/bramveen1/ai-dev-team/issues/364",
                run=run_that_times_out,
            )
        assert "git" in str(exc_info.value).lower()
        assert "timed out" in str(exc_info.value)


# ── Part B: cancel workspace-wipe guard (#350) ────────────────────────────────


class TestDispatchCancelWorkspaceGuard:
    """Workspace is preserved when dispatch_cancel state-file write fails."""

    @staticmethod
    def _seed_dispatch(workspace: Path, *, pid: int = 12345) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "pid").write_text(str(pid))
        (workspace / "started_at").write_text("2026-06-01T12:00:00+00:00")
        (workspace / "budget").write_text("1800")

    def test_workspace_preserved_when_state_write_fails(self, handler, tmp_path: Path) -> None:
        """If _atomic_write raises OSError, workspace must NOT be deleted."""
        dispatch_id = "dispatch-writetest-aabbcc"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        def failing_atomic_write(path: Path, value: str) -> None:
            raise OSError("read-only filesystem")

        import unittest.mock as _mock

        with _mock.patch.object(handler, "_atomic_write", side_effect=failing_atomic_write):
            result = handler.dispatch_cancel(
                dispatch_id=dispatch_id,
                workspace_root=tmp_path,
                sigterm_grace_seconds=0.0,
                _kill_pg_fn=lambda *a: True,
                _is_alive_fn=lambda _: False,
                _sleep_fn=lambda _: None,
            )

        assert workspace.exists(), "workspace must survive when state-file write fails"
        assert result["status"] == "cancelled"
        assert result.get("state_write_failed") is True

    def test_workspace_wiped_when_state_write_succeeds(self, handler, tmp_path: Path) -> None:
        """Normal path: workspace is wiped after successful state write."""
        dispatch_id = "dispatch-normalwipe-ccddee"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.0,
            _kill_pg_fn=lambda *a: True,
            _is_alive_fn=lambda _: False,
            _sleep_fn=lambda _: None,
        )

        assert not workspace.exists(), "workspace must be wiped on successful cancel"
        assert result["status"] == "cancelled"
        assert "state_write_failed" not in result

    def test_state_write_failed_surfaced_in_return_value(self, handler, tmp_path: Path) -> None:
        """state_write_failed key is present (and True) when write fails."""
        dispatch_id = "dispatch-surfacedfail-112233"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        import unittest.mock as _mock

        with _mock.patch.object(
            handler,
            "_atomic_write",
            side_effect=OSError("permission denied"),
        ):
            result = handler.dispatch_cancel(
                dispatch_id=dispatch_id,
                workspace_root=tmp_path,
                sigterm_grace_seconds=0.0,
                _kill_pg_fn=lambda *a: True,
                _is_alive_fn=lambda _: False,
                _sleep_fn=lambda _: None,
            )

        assert result.get("state_write_failed") is True

    def test_state_write_failed_absent_on_success(self, handler, tmp_path: Path) -> None:
        """state_write_failed must not appear in the result when write succeeds."""
        dispatch_id = "dispatch-nofailedkey-445566"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        result = handler.dispatch_cancel(
            dispatch_id=dispatch_id,
            workspace_root=tmp_path,
            sigterm_grace_seconds=0.0,
            _kill_pg_fn=lambda *a: True,
            _is_alive_fn=lambda _: False,
            _sleep_fn=lambda _: None,
        )

        assert "state_write_failed" not in result

    def test_slot_released_even_when_state_write_fails(self, handler, tmp_path: Path) -> None:
        """Slot must be released regardless of state-write outcome."""
        dispatch_id = "dispatch-slotrelease-778899"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        slots_dir.mkdir()
        (slots_dir / "slot-0").write_text(dispatch_id)

        import unittest.mock as _mock

        with _mock.patch.object(
            handler,
            "_atomic_write",
            side_effect=OSError("disk full"),
        ):
            handler.dispatch_cancel(
                dispatch_id=dispatch_id,
                workspace_root=tmp_path,
                sigterm_grace_seconds=0.0,
                _kill_pg_fn=lambda *a: True,
                _is_alive_fn=lambda _: False,
                _sleep_fn=lambda _: None,
            )

        assert not (slots_dir / "slot-0").exists(), "slot must be released even after state-write failure"

    def test_state_write_failure_logged_at_error_level(self, handler, tmp_path: Path) -> None:
        """Write failure must be logged at ERROR (not WARNING) level."""
        dispatch_id = "dispatch-loglevel-aabbcc"
        workspace = tmp_path / dispatch_id
        self._seed_dispatch(workspace)

        import logging
        import unittest.mock as _mock

        with _mock.patch.object(
            handler,
            "_atomic_write",
            side_effect=OSError("read-only filesystem"),
        ):
            with (
                _mock.patch.object(handler.logger, "error") as mock_error,
                _mock.patch.object(handler.logger, "warning") as mock_warning,
            ):
                handler.dispatch_cancel(
                    dispatch_id=dispatch_id,
                    workspace_root=tmp_path,
                    sigterm_grace_seconds=0.0,
                    _kill_pg_fn=lambda *a: True,
                    _is_alive_fn=lambda _: False,
                    _sleep_fn=lambda _: None,
                )

        assert mock_error.called, "state-write failure must be logged at error level"
        assert dispatch_id in mock_error.call_args[0][1], "dispatch_id must appear in the log call"
        assert not any("could not write state files" in str(c) for c in mock_warning.call_args_list), (
            "state-write failure must not be logged at warning level"
        )

        _ = logging  # imported for clarity
