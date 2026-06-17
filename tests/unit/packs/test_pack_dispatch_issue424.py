"""Unit tests for issue #424: early-error paths write error_reason before exitcode.

Covers cause-before-exitcode invariant for:
- clone_failed
- quota_locked
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler(module_name: str = "_test_pack_dispatch_issue424_handler"):
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(module_name, PACK_DIR / "handler.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 55000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


def _base_kwargs(tmp_path: Path) -> dict:
    return dict(
        issue_url="https://github.com/bramveen1/ai-dev-team/issues/424",
        channel="",
        thread_ts="",
        agent="sam",
        workspace_root=tmp_path,
        _seed_auth_fn=_no_op_seed_auth,
        _slack_token=None,
        _approval_cfg=_NO_GATE_CFG,
        popen=_FakePopen,
        supervision_mode="inline",
        _dispatch_token_path=tmp_path / "no.token",
    )


def _find_workspace(tmp_path: Path) -> Path:
    dispatch_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("dispatch-")]
    assert dispatch_dirs, "no dispatch workspace created"
    return dispatch_dirs[0]


# ── clone_failed ──────────────────────────────────────────────────────────────


class TestCloneFailedErrorReasonMarker:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_error_reason_written_on_clone_failed(self, handler, tmp_path: Path) -> None:
        """clone_failed path must write error_reason before exitcode."""

        def failing_clone(workspace, issue_url):
            raise RuntimeError("git clone failed: authentication failed")

        result = handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            _clone_repo_fn=failing_clone,
        )

        assert result["status"] == "error"
        assert result["reason"] == "clone_failed"

        workspace = _find_workspace(tmp_path)
        assert (workspace / "error_reason").exists(), "error_reason marker must be written"
        assert (workspace / "exitcode").exists(), "exitcode must be written"
        assert (workspace / "error_reason").read_text().strip() == "clone_failed"

    def test_error_reason_precedes_exitcode_on_clone_failed(self, handler, tmp_path: Path) -> None:
        """error_reason must be written strictly before exitcode (cause-before-exitcode)."""
        write_order: list[str] = []
        real_atomic_write = handler._atomic_write

        def recording_write(path, content):
            write_order.append(path.name)
            real_atomic_write(path, content)

        def failing_clone(workspace, issue_url):
            raise RuntimeError("git clone failed: network error")

        with patch.object(handler, "_atomic_write", side_effect=recording_write):
            handler.dispatch_issue(
                **_base_kwargs(tmp_path),
                _clone_repo_fn=failing_clone,
            )

        assert "error_reason" in write_order, "error_reason must be written"
        assert "exitcode" in write_order, "exitcode must be written"
        er_idx = write_order.index("error_reason")
        ec_idx = write_order.index("exitcode")
        assert er_idx < ec_idx, f"error_reason (pos {er_idx}) must precede exitcode (pos {ec_idx})"

    def test_error_reason_value_matches_reason_field_on_clone_failed(self, handler, tmp_path: Path) -> None:
        """error_reason file value must match the reason in the result dict."""

        def failing_clone(workspace, issue_url):
            raise RuntimeError("git clone failed: timeout")

        result = handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            _clone_repo_fn=failing_clone,
        )

        workspace = _find_workspace(tmp_path)
        assert (workspace / "error_reason").read_text().strip() == result["reason"]


# ── quota_locked ──────────────────────────────────────────────────────────────


class TestQuotaLockedErrorReasonMarker:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def _make_locked_quota_mock(self) -> MagicMock:
        mock_quota = MagicMock()
        mock_quota.is_locked.return_value = (True, "2026-06-17T14:00:00+00:00")
        mock_quota.window_state.return_value = (0.0, 0, None)
        mock_quota.load_config.return_value = {"threshold_usd": 50.0, "window_hours": 5.0}
        return mock_quota

    def test_error_reason_written_on_quota_locked(self, handler, tmp_path: Path) -> None:
        """quota_locked path must write error_reason before exitcode."""
        mock_quota = self._make_locked_quota_mock()

        with (
            patch.object(handler, "_QUOTA_AVAILABLE", True),
            patch.object(handler, "_quota", mock_quota),
            patch.object(handler, "_load_quota_config", return_value={"window_hours": 5.0}),
        ):
            result = handler.dispatch_issue(
                **_base_kwargs(tmp_path),
                _clone_repo_fn=lambda ws, url: ws / "repo",
            )

        assert result["status"] == "error"
        assert result["reason"] == "quota_locked"

        workspace = _find_workspace(tmp_path)
        assert (workspace / "error_reason").exists(), "error_reason marker must be written"
        assert (workspace / "exitcode").exists(), "exitcode must be written"
        assert (workspace / "error_reason").read_text().strip() == "quota_locked"

    def test_error_reason_precedes_exitcode_on_quota_locked(self, handler, tmp_path: Path) -> None:
        """error_reason must be written strictly before exitcode on quota_locked."""
        write_order: list[str] = []
        real_atomic_write = handler._atomic_write

        def recording_write(path, content):
            write_order.append(path.name)
            real_atomic_write(path, content)

        mock_quota = self._make_locked_quota_mock()

        with (
            patch.object(handler, "_QUOTA_AVAILABLE", True),
            patch.object(handler, "_quota", mock_quota),
            patch.object(handler, "_load_quota_config", return_value={"window_hours": 5.0}),
            patch.object(handler, "_atomic_write", side_effect=recording_write),
        ):
            handler.dispatch_issue(
                **_base_kwargs(tmp_path),
                _clone_repo_fn=lambda ws, url: ws / "repo",
            )

        assert "error_reason" in write_order, "error_reason must be written"
        assert "exitcode" in write_order, "exitcode must be written"
        er_idx = write_order.index("error_reason")
        ec_idx = write_order.index("exitcode")
        assert er_idx < ec_idx, f"error_reason (pos {er_idx}) must precede exitcode (pos {ec_idx})"

    def test_error_reason_value_matches_reason_field_on_quota_locked(self, handler, tmp_path: Path) -> None:
        """error_reason file value must match the reason in the result dict."""
        mock_quota = self._make_locked_quota_mock()

        with (
            patch.object(handler, "_QUOTA_AVAILABLE", True),
            patch.object(handler, "_quota", mock_quota),
            patch.object(handler, "_load_quota_config", return_value={"window_hours": 5.0}),
        ):
            result = handler.dispatch_issue(
                **_base_kwargs(tmp_path),
                _clone_repo_fn=lambda ws, url: ws / "repo",
            )

        workspace = _find_workspace(tmp_path)
        assert (workspace / "error_reason").read_text().strip() == result["reason"]
