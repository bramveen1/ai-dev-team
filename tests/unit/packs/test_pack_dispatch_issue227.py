"""Unit tests for issue #227: machine-user GitHub identities for the dispatch worker.

Covers:

- ``_read_machine_user_token``: reads a real token, skips comment lines, returns
  None for absent/placeholder-only files.
- ``_seed_dispatch_identity``: writes ``.env`` (0600) and ``.gitconfig`` with
  the correct content.
- ``dispatch_issue`` dispatch identity injection:
  - ``GH_TOKEN`` / ``GITHUB_TOKEN`` from the host environment are stripped from
    ``extra_env`` (host PAT must never reach the worker).
  - When a dispatch token exists the worker env gets the dispatch token and
    ``GIT_CONFIG_GLOBAL`` pointing at ``workspace/.gitconfig``.
  - When the token is absent the host PAT is still stripped; no ``.env`` is written.
  - Identity injection is skipped when ``exec_override`` is set.
- ``dispatch_health`` ``gh_auth_ok`` field:
  - Happy path → ``gh_auth_ok: True``, no ``gh_auth_detail``.
  - Non-zero exit → ``gh_auth_ok: False`` with ``gh_auth_detail``.
  - ``gh`` binary missing → ``gh_auth_ok: False``.
- ``_check_gh_auth`` injectable ``run`` parameter works.
"""

from __future__ import annotations

import importlib.util
import logging
import os
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
        "_test_pack_dispatch_issue227_handler",
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


# D-7: approval gate is fail-closed. Tests not covering gating bypass it.
_NO_GATE_CFG: dict = {"require_always": False, "destructive_keywords": []}


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


class _FakePopen:
    """Minimal Popen stub that writes exitcode=0 on wait()."""

    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 99000 + len(_FakePopen.instances)
        self.cwd = kwargs.get("cwd")
        self.env = kwargs.get("env")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


# ── _read_machine_user_token ─────────────────────────────────────────────


class TestReadMachineUserToken:
    def test_reads_real_token(self, handler, tmp_path: Path) -> None:
        token_file = tmp_path / "gh-aidt-dispatch.token"
        token_file.write_text("ghp_realtoken123456789\n")
        assert handler._read_machine_user_token(token_file) == "ghp_realtoken123456789"

    def test_skips_comment_lines(self, handler, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("# This is a placeholder\n# Another comment\nghp_actualtoken\n")
        assert handler._read_machine_user_token(token_file) == "ghp_actualtoken"

    def test_returns_none_for_all_comments(self, handler, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("# REPLACE_WITH_REAL_TOKEN\n")
        assert handler._read_machine_user_token(token_file) is None

    def test_returns_none_for_absent_file(self, handler, tmp_path: Path) -> None:
        assert handler._read_machine_user_token(tmp_path / "nonexistent.token") is None

    def test_returns_none_for_empty_file(self, handler, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("")
        assert handler._read_machine_user_token(token_file) is None

    def test_strips_whitespace(self, handler, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("  ghp_padded  \n")
        assert handler._read_machine_user_token(token_file) == "ghp_padded"

    def test_permission_error_returns_none_logs_warning(self, handler, tmp_path: Path, caplog) -> None:
        if os.getuid() == 0:
            pytest.skip("cannot simulate PermissionError as root")
        token_file = tmp_path / "restricted.token"
        token_file.write_text("ghp_secret")
        token_file.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
                result = handler._read_machine_user_token(token_file)
            assert result is None
            assert "unreadable" in caplog.text
        finally:
            token_file.chmod(0o644)

    def test_empty_file_logs_warning(self, handler, tmp_path: Path, caplog) -> None:
        token_file = tmp_path / "empty.token"
        token_file.write_text("")
        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            result = handler._read_machine_user_token(token_file)
        assert result is None
        assert "empty" in caplog.text


# ── _seed_dispatch_identity ──────────────────────────────────────────────


class TestSeedDispatchIdentity:
    def test_writes_env_file_0600(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._seed_dispatch_identity(workspace, "ghp_testtoken")
        env_path = workspace / ".env"
        assert env_path.exists()
        assert oct(env_path.stat().st_mode & 0o777) == oct(0o600)

    def test_env_file_contains_both_token_vars(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._seed_dispatch_identity(workspace, "ghp_mytoken")
        content = (workspace / ".env").read_text()
        assert "GH_TOKEN=ghp_mytoken" in content
        assert "GITHUB_TOKEN=ghp_mytoken" in content

    def test_writes_gitconfig(self, handler, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        handler._seed_dispatch_identity(workspace, "ghp_tok")
        gitconfig = (workspace / ".gitconfig").read_text()
        assert "aidt-dispatch" in gitconfig
        assert "Dispatch (aidt-dispatch)" in gitconfig
        assert "aidt-dispatch@users.noreply.github.com" in gitconfig


# ── dispatch_issue identity injection ───────────────────────────────────


class TestDispatchIssueIdentityInjection:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def _base_kwargs(self, tmp_path: Path, dispatch_token_path: Path | None = None) -> dict:
        return dict(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/1",
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
            _dispatch_token_path=dispatch_token_path,
        )

    def test_host_pat_stripped_when_token_present(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_dispatchtoken\n")

        handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        assert _FakePopen.instances, "expected a Popen call"
        env = _FakePopen.instances[-1].env
        assert env is not None
        assert env.get("GH_TOKEN") == "ghp_dispatchtoken"
        assert env.get("GITHUB_TOKEN") == "ghp_dispatchtoken"

    def test_host_pat_inherited_when_token_absent(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        missing_token = tmp_path / "no_such.token"

        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            handler.dispatch_issue(**self._base_kwargs(tmp_path, missing_token))

        assert _FakePopen.instances
        env = _FakePopen.instances[-1].env
        assert env is not None
        # No strip when token unavailable — worker inherits host PAT.
        assert env.get("GH_TOKEN") == "ghp_hostpat"
        assert env.get("GITHUB_TOKEN") == "ghp_hostpat"
        assert "dispatch token unavailable" in caplog.text

    def test_permission_error_no_strip_warning_logged(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        if os.getuid() == 0:
            pytest.skip("cannot simulate PermissionError as root")
        monkeypatch.setenv("GH_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        token_file = tmp_path / "restricted.token"
        token_file.write_text("ghp_secret")
        token_file.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
                handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))
            env = _FakePopen.instances[-1].env
            assert env is not None
            assert env.get("GH_TOKEN") == "ghp_hostpat"
            assert env.get("GITHUB_TOKEN") == "ghp_hostpat"
            assert "dispatch token unavailable" in caplog.text
        finally:
            token_file.chmod(0o644)

    def test_empty_token_file_no_strip_warning_logged(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        token_file = tmp_path / "empty.token"
        token_file.write_text("")

        with caplog.at_level(logging.WARNING, logger="dispatch.handler"):
            handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        assert _FakePopen.instances
        env = _FakePopen.instances[-1].env
        assert env is not None
        assert env.get("GH_TOKEN") == "ghp_hostpat"
        assert env.get("GITHUB_TOKEN") == "ghp_hostpat"
        assert "dispatch token unavailable" in caplog.text

    def test_gitconfig_global_set_when_token_present(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_dispatchtoken\n")

        handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        env = _FakePopen.instances[-1].env
        assert env is not None
        git_cfg = env.get("GIT_CONFIG_GLOBAL", "")
        assert git_cfg.endswith(".gitconfig"), f"GIT_CONFIG_GLOBAL should point to .gitconfig, got {git_cfg!r}"

    def test_env_file_written_at_workspace_dot_env(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_dispatchtoken\n")

        result = handler.dispatch_issue(**self._base_kwargs(tmp_path, token_file))

        workspace = Path(result["workspace"])
        env_path = workspace / ".env"
        assert env_path.exists(), ".env must be written to the dispatch workspace"
        assert oct(env_path.stat().st_mode & 0o777) == oct(0o600), ".env must be 0600"

    def test_identity_injection_skipped_when_exec_override_set(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """exec_override (test/smoke-probe mode) must skip identity injection."""
        monkeypatch.setenv("GH_TOKEN", "ghp_hostpat")
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-fake")

        token_file = tmp_path / "dispatch.token"
        token_file.write_text("ghp_dispatchtoken\n")

        kwargs = self._base_kwargs(tmp_path, token_file)
        kwargs["exec_override"] = ["sleep", "0"]

        handler.dispatch_issue(**kwargs)

        env = _FakePopen.instances[-1].env
        # exec_override skips identity injection, so env is None (inherit parent)
        assert env is None


# ── dispatch_health gh_auth_ok ───────────────────────────────────────────


def _make_claude_run(version_completed: Any, probe_completed: Any) -> Any:
    """Fake subprocess.run for claude --version and claude -p."""

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if len(argv) >= 2 and argv[1] == "--version":
            return version_completed
        if "-p" in argv:
            return probe_completed
        raise AssertionError(f"unexpected argv: {argv!r}")

    return fake_run


class TestDispatchHealthGhAuthOk:
    def _base_run(self) -> Any:
        return _make_claude_run(
            _completed(stdout="2.1.0\n"),
            _completed(stdout='{"is_error": false, "result": "hello"}'),
        )

    def test_gh_auth_ok_true_on_success(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=self._base_run(),
            workspace_root=tmp_path,
            _gh_run=lambda *a, **kw: _completed(returncode=0),
        )
        assert result["gh_auth_ok"] is True
        assert "gh_auth_detail" not in result

    def test_gh_auth_ok_false_on_nonzero_exit(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=self._base_run(),
            workspace_root=tmp_path,
            _gh_run=lambda *a, **kw: _completed(stderr="Your token has expired.", returncode=1),
        )
        assert result["gh_auth_ok"] is False
        assert "gh_auth_detail" in result
        assert "expired" in result["gh_auth_detail"]

    def test_gh_auth_ok_false_when_gh_missing(self, handler, tmp_path: Path) -> None:
        def missing_gh(*args, **kwargs):
            raise FileNotFoundError("gh not found")

        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=self._base_run(),
            workspace_root=tmp_path,
            _gh_run=missing_gh,
        )
        assert result["gh_auth_ok"] is False
        assert "gh_auth_detail" in result

    def test_other_health_fields_unaffected(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_health(
            which=lambda _: "/usr/local/bin/claude",
            run=self._base_run(),
            workspace_root=tmp_path,
            _gh_run=lambda *a, **kw: _completed(returncode=0),
        )
        assert result["workspace_volume_writable"] is True
        assert result["sonnet_probe_ok"] is True
        assert result["cli_version"] == "2.1.0"
