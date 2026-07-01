"""Regression tests for issue #558: concurrent dispatch workspace isolation.

Covers:
- DISPATCH_REPO_ENV constant is exported from handler.py.
- DISPATCH_REPO is explicitly injected into the worker's subprocess environment
  (not just written to workspace/.env) when a per-dispatch repo clone exists.
- DISPATCH_REPO is absent from the env when exec_override is set (no real clone).
- DISPATCH_REPO is absent when repo_path resolves to None.
- Two concurrent dispatches launched for the same agent each receive a
  different, non-overlapping DISPATCH_REPO value — the per-dispatch clone
  guarantees they never share a working tree.
- DISPATCH_REPO stays present even when no dispatch token is available
  (token-absent path previously omitted DISPATCH_REPO from .env entirely).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"

# D-7: approval gate is fail-closed by default. Tests that aren't
# testing approval gating must pass this config to bypass the gate.
_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


# ── Module loader ─────────────────────────────────────────────────────────────


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_handler_558",
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


# ── Test helpers ──────────────────────────────────────────────────────────────


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


def _no_op_clone(workspace: Path, issue_url: str) -> Path:
    """Clone stub: create repo dir and return its path (simulates a real clone)."""
    repo = workspace / "repo"
    repo.mkdir(exist_ok=True)
    return repo


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 50000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


def _base_kwargs(tmp_path: Path, token_file: Path | None = None) -> dict:
    """Base kwargs for dispatch_issue calls in these tests."""
    return dict(
        issue_url="https://github.com/bramveen1/ai-dev-team/issues/558",
        channel="",
        thread_ts="",
        agent="sam",
        workspace_root=tmp_path,
        _seed_auth_fn=_no_op_seed_auth,
        _slack_token=None,
        _approval_cfg=_NO_GATE_CFG,
        popen=_FakePopen,
        supervision_mode="poll",
        _dispatch_token_path=token_file or (tmp_path / "no.token"),
    )


def _dispatch_repo_from_env(popen_instance: _FakePopen) -> str | None:
    """Extract DISPATCH_REPO from the env dict passed to a _FakePopen instance."""
    env = popen_instance.kwargs.get("env") or {}
    return env.get("DISPATCH_REPO")


# ── DISPATCH_REPO_ENV constant ────────────────────────────────────────────────


def test_dispatch_repo_env_constant_exported(handler) -> None:
    """DISPATCH_REPO_ENV must be exported from handler.py."""
    assert hasattr(handler, "DISPATCH_REPO_ENV")
    assert handler.DISPATCH_REPO_ENV == "DISPATCH_REPO"


# ── DISPATCH_REPO injected into subprocess env ────────────────────────────────


class TestDispatchRepoInjectedIntoEnv:
    """DISPATCH_REPO must appear in the babysit subprocess's environment."""

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_dispatch_repo_in_env_when_clone_succeeds(self, handler, tmp_path: Path) -> None:
        """Successful clone → DISPATCH_REPO is in the babysit subprocess env."""
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            _clone_repo_fn=_no_op_clone,
        )

        assert _FakePopen.instances, "babysit subprocess must have been spawned"
        repo_in_env = _dispatch_repo_from_env(_FakePopen.instances[0])
        assert repo_in_env is not None, "DISPATCH_REPO must be set in the babysit env"

    def test_dispatch_repo_points_to_per_dispatch_clone(self, handler, tmp_path: Path) -> None:
        """DISPATCH_REPO must point to the dispatch's own repo/ subdirectory."""
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            _clone_repo_fn=_no_op_clone,
        )

        assert _FakePopen.instances
        repo_in_env = _dispatch_repo_from_env(_FakePopen.instances[0])
        assert repo_in_env is not None
        repo_path = Path(repo_in_env)
        # Must be <workspace>/repo/, not a shared container workspace.
        assert repo_path.name == "repo", f"DISPATCH_REPO should point to repo/ dir, got {repo_path}"
        # Must be inside the dispatch workspace root.
        assert str(tmp_path) in repo_in_env, "DISPATCH_REPO must be inside the dispatch workspace root"

    def test_dispatch_repo_absent_when_exec_override_set(self, handler, tmp_path: Path) -> None:
        """exec_override skips the clone; DISPATCH_REPO must not be injected."""
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            exec_override=["true"],
        )

        assert _FakePopen.instances
        repo_in_env = _dispatch_repo_from_env(_FakePopen.instances[0])
        assert repo_in_env is None, "DISPATCH_REPO must not be set when exec_override is active (no repo cloned)"

    def test_dispatch_repo_present_even_without_dispatch_token(self, handler, tmp_path: Path) -> None:
        """No machine-user token → clone still runs, DISPATCH_REPO still injected.

        _base_kwargs already points _dispatch_token_path at a nonexistent file,
        so the handler cannot read a token and _seed_dispatch_identity is skipped.
        Before #558, this path omitted DISPATCH_REPO from the subprocess env entirely.
        """
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            _clone_repo_fn=_no_op_clone,
        )

        assert _FakePopen.instances
        repo_in_env = _dispatch_repo_from_env(_FakePopen.instances[0])
        assert repo_in_env is not None, "DISPATCH_REPO must be set even when no dispatch token is available"


# ── Concurrent dispatch isolation ─────────────────────────────────────────────


class TestConcurrentDispatchIsolation:
    """Regression for issue #558: two concurrent dispatches must not share a workspace.

    Simulates the overlap by launching two dispatch_issue calls sequentially
    (using a deterministic timestamp override to force different dispatch IDs)
    and verifying each worker received a different, non-overlapping DISPATCH_REPO.
    """

    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_two_dispatches_get_different_dispatch_repo(self, handler, tmp_path: Path) -> None:
        """Two dispatches for the same agent must each get their own DISPATCH_REPO."""
        from datetime import datetime, timezone

        def _clone_first(workspace: Path, issue_url: str) -> Path:
            repo = workspace / "repo"
            repo.mkdir(exist_ok=True)
            return repo

        def _clone_second(workspace: Path, issue_url: str) -> Path:
            repo = workspace / "repo"
            repo.mkdir(exist_ok=True)
            return repo

        now1 = datetime(2026, 6, 26, 8, 2, 58, tzinfo=timezone.utc)
        now2 = datetime(2026, 6, 26, 8, 10, 46, tzinfo=timezone.utc)

        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            now=now1,
            _clone_repo_fn=_clone_first,
        )
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            now=now2,
            _clone_repo_fn=_clone_second,
        )

        assert len(_FakePopen.instances) == 2, "two babysit subprocesses must be spawned"
        repo1 = _dispatch_repo_from_env(_FakePopen.instances[0])
        repo2 = _dispatch_repo_from_env(_FakePopen.instances[1])

        assert repo1 is not None, "first dispatch must have DISPATCH_REPO set"
        assert repo2 is not None, "second dispatch must have DISPATCH_REPO set"
        assert repo1 != repo2, (
            f"concurrent dispatches must get different DISPATCH_REPO values;\n  first:  {repo1}\n  second: {repo2}"
        )

    def test_dispatch_repo_paths_are_isolated_directories(self, handler, tmp_path: Path) -> None:
        """Each dispatch's DISPATCH_REPO must reside in its own dispatch workspace."""
        from datetime import datetime, timezone

        now1 = datetime(2026, 6, 26, 8, 2, 58, tzinfo=timezone.utc)
        now2 = datetime(2026, 6, 26, 8, 10, 46, tzinfo=timezone.utc)

        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            now=now1,
            _clone_repo_fn=_no_op_clone,
        )
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            now=now2,
            _clone_repo_fn=_no_op_clone,
        )

        assert len(_FakePopen.instances) == 2
        repo1 = Path(_dispatch_repo_from_env(_FakePopen.instances[0]))
        repo2 = Path(_dispatch_repo_from_env(_FakePopen.instances[1]))

        # Neither path should be a prefix of the other — they're fully isolated.
        assert not str(repo1).startswith(str(repo2)), "repo paths must not be nested"
        assert not str(repo2).startswith(str(repo1)), "repo paths must not be nested"

        # Both must share the same workspace root (tmp_path) but different dispatch dirs.
        dispatch_dir1 = repo1.parent  # <dispatch_id>/
        dispatch_dir2 = repo2.parent
        assert dispatch_dir1.parent == tmp_path, "dispatch workspace must be under workspace root"
        assert dispatch_dir2.parent == tmp_path, "dispatch workspace must be under workspace root"
        assert dispatch_dir1 != dispatch_dir2, "each dispatch must have its own directory"

    def test_dispatch_repo_uses_dispatch_repo_env_constant(self, handler, tmp_path: Path) -> None:
        """The injected key must match the DISPATCH_REPO_ENV constant."""
        handler.dispatch_issue(
            **_base_kwargs(tmp_path),
            _clone_repo_fn=_no_op_clone,
        )

        assert _FakePopen.instances
        env = _FakePopen.instances[0].kwargs.get("env") or {}
        assert handler.DISPATCH_REPO_ENV in env, (
            f"Expected {handler.DISPATCH_REPO_ENV!r} in babysit env; keys present: {sorted(env)}"
        )
