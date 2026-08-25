"""Unit tests for issue #414: missing dispatch token must hard-fail the clone.

Issue #416 wired the aidt-dispatch token into ``_clone_repo_into_workspace``
but let a missing token silently fall through to an anonymous clone. This
worked for the public ``ai-dev-team`` repo but produced opaque git errors for
private repos. #414 tightens the policy: when ``dispatch_issue`` is about to
run the real clone (no ``_clone_repo_fn`` test seam) and no dispatch token is
available, it must fail immediately with a structured ``clone_failed`` error
that clearly names the missing token, instead of ever attempting an anonymous
clone.

Covers:

- Real clone path (no ``_clone_repo_fn``) + no token → ``clone_failed`` with
  a detail naming the missing token, before any subprocess is spawned.
- The failure happens before slot acquisition (no slot files created).
- Real clone path + token present is unaffected (still delegates to
  ``_clone_repo_into_workspace``, unit-tested separately in #416's suite).
- The ``_clone_repo_fn`` test seam still bypasses the gate entirely (so
  existing tests across the suite that stub the clone without a token keep
  working unchanged).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_issue414_handler",
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
        self.pid = 88000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


class TestCloneRefusesAnonymousWhenTokenMissing:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def _base_kwargs(self, tmp_path: Path, dispatch_token_path: Path) -> dict:
        return dict(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/414",
            channel="",
            thread_ts="",
            agent="sam",
            workspace_root=tmp_path,
            _seed_auth_fn=_no_op_seed_auth,
            _slack_token=None,
            _approval_cfg=_NO_GATE_CFG,
            popen=_FakePopen,
            supervision_mode="inline",
            _dispatch_token_path=dispatch_token_path,
        )

    def test_missing_token_returns_clone_failed(self, handler, tmp_path: Path) -> None:
        missing_token = tmp_path / "no_such.token"

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, missing_token))

        assert result["status"] == "error"
        assert result["reason"] == "clone_failed"

    def test_missing_token_detail_names_the_token(self, handler, tmp_path: Path) -> None:
        missing_token = tmp_path / "no_such.token"

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, missing_token))

        assert "token" in result["detail"].lower()
        assert str(missing_token) in result["detail"]

    def test_missing_token_does_not_spawn_babysit(self, handler, tmp_path: Path) -> None:
        missing_token = tmp_path / "no_such.token"

        handler.dispatch_issue(**self._base_kwargs(tmp_path, missing_token))

        assert _FakePopen.instances == []

    def test_missing_token_fails_before_slot_acquire(self, handler, tmp_path: Path) -> None:
        missing_token = tmp_path / "no_such.token"

        handler.dispatch_issue(**self._base_kwargs(tmp_path, missing_token))

        slots_dir = tmp_path / ".slots"
        if slots_dir.exists():
            assert list(slots_dir.iterdir()) == []

    def test_empty_token_file_also_refused(self, handler, tmp_path: Path) -> None:
        """A comment-only/blank token file reads as ``None`` — same as absent."""
        empty_token = tmp_path / "blank.token"
        empty_token.write_text("# placeholder, not seeded yet\n")

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, empty_token))

        assert result["status"] == "error"
        assert result["reason"] == "clone_failed"


class TestCloneStillWorksWhenTokenPresent:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_token_present_reaches_real_clone_fn(self, handler, tmp_path: Path, monkeypatch) -> None:
        """With a token, the gate is skipped and the real clone helper runs."""
        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_realtoken\n")

        calls: list[tuple] = []

        def fake_clone(workspace, url, *, token=None, head_branch=None):
            calls.append((workspace, url, token, head_branch))
            (workspace / "repo").mkdir(parents=True, exist_ok=True)
            return workspace / "repo"

        monkeypatch.setattr(handler, "_clone_repo_into_workspace", fake_clone)

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/414",
            channel="",
            thread_ts="",
            agent="sam",
            workspace_root=tmp_path,
            _seed_auth_fn=_no_op_seed_auth,
            _slack_token=None,
            _approval_cfg=_NO_GATE_CFG,
            popen=_FakePopen,
            supervision_mode="inline",
            _dispatch_token_path=token_file,
        )

        assert result["status"] in ("completed", "launched")
        assert len(calls) == 1
        _, _, used_token, _ = calls[0]
        assert used_token == "ghp_realtoken"


class TestCloneRepoFnSeamBypassesGate:
    """The injectable ``_clone_repo_fn`` test seam is untouched by the gate."""

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_clone_repo_fn_runs_even_without_token(self, handler, tmp_path: Path) -> None:
        missing_token = tmp_path / "no_such.token"
        calls: list[tuple] = []

        def stub_clone(workspace, issue_url):
            calls.append((workspace, issue_url))
            return workspace / "repo"

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/414",
            channel="",
            thread_ts="",
            agent="sam",
            workspace_root=tmp_path,
            _seed_auth_fn=_no_op_seed_auth,
            _slack_token=None,
            _approval_cfg=_NO_GATE_CFG,
            popen=_FakePopen,
            supervision_mode="inline",
            _dispatch_token_path=missing_token,
            _clone_repo_fn=stub_clone,
        )

        assert len(calls) == 1
        assert result["status"] in ("completed", "launched")
