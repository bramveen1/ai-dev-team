"""Regression tests for #394: slot leak when an exception fires between a
successful ``_acquire_slot`` and the ``_launch_*`` call (no babysit spawned
yet, so nothing else will ever release the slot).

``_seed_dispatch_identity`` raising an ``OSError`` (the most likely raiser
per the issue — per-dispatch ``.env``/``.gitconfig`` writes) is the
canonical repro: the fix wraps the whole launch-prep block (command
building, identity seeding, env assembly) in a try/except that releases the
slot via the ownership-checked ``_release_slot_for_dispatch`` before
returning a structured error envelope.
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
        "_test_pack_dispatch_issue394_handler",
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


def _no_op_clone(workspace: Path, issue_url: str) -> Path:
    return workspace / "repo"


# D-7: approval gate is fail-closed. Non-D7 tests bypass it.
_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 91000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


class TestSlotLeakOnLaunchPrepFailure:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def _base_kwargs(self, tmp_path: Path, token_file: Path) -> dict:
        return dict(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/394",
            channel="",
            thread_ts="",
            agent="sam",
            workspace_root=tmp_path,
            _seed_auth_fn=_no_op_seed_auth,
            _clone_repo_fn=_no_op_clone,
            _slack_token=None,
            _approval_cfg=_NO_GATE_CFG,
            popen=_FakePopen,
            supervision_mode="inline",
            _dispatch_token_path=token_file,
        )

    def test_seed_dispatch_identity_raising_restores_slot_count(self, handler, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_faketoken\n")

        def _raise_oserror(workspace, token, *, dispatch_repo=None):
            raise OSError("disk full")

        monkeypatch.setattr(handler, "_seed_dispatch_identity", _raise_oserror)

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        assert result["status"] == "error"
        assert result["reason"] == "launch_prep_failed"

        # No babysit was ever spawned.
        assert len(_FakePopen.instances) == 0

        # The slot must be released — no leaked slot-N file.
        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        held = list(slots_dir.iterdir()) if slots_dir.exists() else []
        assert held == [], f"slot leaked after launch-prep failure: {[p.name for p in held]}"

        # The pool must be fully usable again: POOL_SIZE more dispatches
        # should each acquire a slot without queuing.
        for i in range(handler.POOL_SIZE):
            idx = handler._try_acquire_slot(slots_dir, f"after-leak-{i}")
            assert idx is not None, f"pool wedged after launch-prep failure (dispatch {i} could not acquire)"

    def test_seed_dispatch_identity_raising_writes_terminal_state(self, handler, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_faketoken\n")

        def _raise_oserror(workspace, token, *, dispatch_repo=None):
            raise OSError("disk full")

        monkeypatch.setattr(handler, "_seed_dispatch_identity", _raise_oserror)

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        workspace = Path(result["workspace"])
        assert (workspace / "exitcode").read_text().strip() == str(handler.EXIT_LAUNCH_FAILED)
        assert (workspace / "error_reason").read_text().strip() == "launch_prep_failed"

    def test_build_claude_command_raising_restores_slot_count(self, handler, tmp_path: Path, monkeypatch) -> None:
        """Any launch-prep raiser (not just identity seeding) must release the slot."""
        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_faketoken\n")

        def _raise_valueerror(**kwargs):
            raise ValueError("bad persona template")

        monkeypatch.setattr(handler, "_build_claude_command", _raise_valueerror)

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        assert result["status"] == "error"
        assert result["reason"] == "launch_prep_failed"
        assert len(_FakePopen.instances) == 0

        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        held = list(slots_dir.iterdir()) if slots_dir.exists() else []
        assert held == [], f"slot leaked after launch-prep failure: {[p.name for p in held]}"
