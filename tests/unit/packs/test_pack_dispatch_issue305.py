"""Unit tests for issue #305: deploy-pause sentinel gate.

Covers:

- ``_check_deploy_pause``:
  - Absent sentinel → returns None (proceed).
  - Live sentinel (age < 1800s) → returns reject dict with reason=deploy_pause.
  - Stale sentinel (age > 1800s) → returns None, logs WARNING.
  - Malformed sentinel JSON → returns None, logs WARNING.
  - OSError reading sentinel → returns None, logs WARNING.
  - ``DEPLOY_PAUSE_ENABLED=0`` → returns None regardless of sentinel presence.

- ``dispatch_issue`` integration:
  - Live sentinel present → returns ``{status: error, reason: deploy_pause}``
    with "deploy in progress, draining" in the detail.
  - Stale sentinel (age > 1800s) → dispatch proceeds normally (allowed).
  - ``DEPLOY_PAUSE_ENABLED=0`` → dispatch proceeds normally (sentinel ignored).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_issue305_handler",
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


def _write_sentinel(root: Path, *, age_seconds: float = 0.0) -> None:
    """Write a .deploy-pause sentinel to *root* with the given age."""
    started_at = time.time() - age_seconds
    sentinel = {"started_at": int(started_at), "deploy_sha": "abc123", "pid": 9999}
    (root / ".deploy-pause").write_text(json.dumps(sentinel))


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


def _no_op_clone(workspace: Path, issue_url: str) -> Path:
    return workspace / "repo"


_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


# ── _check_deploy_pause unit tests ───────────────────────────────────────────


class TestCheckDeployPause:
    def test_absent_sentinel_returns_none(self, handler, tmp_path: Path) -> None:
        result = handler._check_deploy_pause(tmp_path)
        assert result is None

    def test_live_sentinel_returns_reject(self, handler, tmp_path: Path) -> None:
        _write_sentinel(tmp_path, age_seconds=30.0)
        result = handler._check_deploy_pause(tmp_path, now_ts=time.time())
        assert result is not None
        assert result["status"] == "error"
        assert result["reason"] == "deploy_pause"
        assert "deploy in progress" in result["detail"]
        assert "re-fire" in result["detail"]

    def test_stale_sentinel_returns_none_and_logs_warning(self, handler, tmp_path: Path, caplog) -> None:
        _write_sentinel(tmp_path, age_seconds=1801.0)
        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            result = handler._check_deploy_pause(tmp_path, now_ts=time.time())
        assert result is None
        assert any("stale" in r.message for r in caplog.records)

    def test_malformed_sentinel_returns_none_and_logs_warning(self, handler, tmp_path: Path, caplog) -> None:
        (tmp_path / ".deploy-pause").write_text("not-json{{{")
        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            result = handler._check_deploy_pause(tmp_path)
        assert result is None
        assert any("malformed" in r.message for r in caplog.records)

    def test_oserror_reading_sentinel_returns_none_and_logs_warning(self, handler, tmp_path: Path, caplog) -> None:
        sentinel_path = tmp_path / ".deploy-pause"
        sentinel_path.mkdir()  # directory, not a file → read_text() raises IsADirectoryError
        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            result = handler._check_deploy_pause(tmp_path)
        assert result is None
        assert any("failed to read sentinel" in r.message for r in caplog.records)

    def test_disabled_via_env_returns_none_even_with_live_sentinel(self, handler, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(handler.DEPLOY_PAUSE_ENABLED_ENV, "0")
        _write_sentinel(tmp_path, age_seconds=10.0)
        result = handler._check_deploy_pause(tmp_path, now_ts=time.time())
        assert result is None

    def test_exactly_at_stale_boundary_is_stale(self, handler, tmp_path: Path) -> None:
        _write_sentinel(tmp_path, age_seconds=0.0)
        now_ts = time.time() + handler.DEPLOY_PAUSE_STALE_SECONDS + 1
        result = handler._check_deploy_pause(tmp_path, now_ts=now_ts)
        assert result is None

    def test_just_below_stale_boundary_is_live(self, handler, tmp_path: Path) -> None:
        _write_sentinel(tmp_path, age_seconds=0.0)
        now_ts = time.time() + handler.DEPLOY_PAUSE_STALE_SECONDS - 1
        result = handler._check_deploy_pause(tmp_path, now_ts=now_ts)
        assert result is not None
        assert result["reason"] == "deploy_pause"


# ── dispatch_issue integration tests ─────────────────────────────────────────


class TestDispatchIssuePauseSentinel:
    """Verify dispatch_issue honours the sentinel gate end-to-end."""

    def _base_kwargs(self, tmp_path: Path) -> dict:
        return dict(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/305",
            channel="C123",
            thread_ts="1234.5678",
            agent="sam",
            workspace_root=tmp_path,
            _approved=True,
            _approval_cfg=_NO_GATE_CFG,
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone,
            _slack_token="xoxb-test",
        )

    def test_live_sentinel_rejects_dispatch(self, handler, tmp_path: Path) -> None:
        _write_sentinel(tmp_path, age_seconds=60.0)
        result = handler.dispatch_issue(**self._base_kwargs(tmp_path))
        assert result["status"] == "error"
        assert result["reason"] == "deploy_pause"
        assert "deploy in progress" in result["detail"]

    def test_live_sentinel_rejects_without_creating_workspace(self, handler, tmp_path: Path) -> None:
        _write_sentinel(tmp_path, age_seconds=60.0)
        handler.dispatch_issue(**self._base_kwargs(tmp_path))
        # No dispatch workspace dirs should have been created.
        dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("dispatch-")]
        assert dirs == []

    def test_stale_sentinel_allows_dispatch(self, handler, tmp_path: Path, monkeypatch) -> None:
        _write_sentinel(tmp_path, age_seconds=1900.0)

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                self.pid = 12345
                if kwargs.get("cwd"):
                    (Path(kwargs["cwd"]) / "exitcode").write_text("0")

            def wait(self):
                return 0

        result = handler.dispatch_issue(
            **self._base_kwargs(tmp_path),
            popen=_FakePopen,
            supervision_mode="inline",
        )
        assert result["status"] in {"completed", "launched", "error"}
        assert result.get("reason") != "deploy_pause"

    def test_disabled_env_allows_dispatch(self, handler, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(handler.DEPLOY_PAUSE_ENABLED_ENV, "0")
        _write_sentinel(tmp_path, age_seconds=10.0)

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                self.pid = 12346
                if kwargs.get("cwd"):
                    (Path(kwargs["cwd"]) / "exitcode").write_text("0")

            def wait(self):
                return 0

        result = handler.dispatch_issue(
            **self._base_kwargs(tmp_path),
            popen=_FakePopen,
            supervision_mode="inline",
        )
        assert result.get("reason") != "deploy_pause"
