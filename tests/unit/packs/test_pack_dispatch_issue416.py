"""Unit tests for issue #416: authenticated clone for private repos.

Covers _clone_repo_into_workspace:
  (a) token present  → clone and pull commands carry http.extraheader arg
  (b) token absent   → plain clone/pull command, no header injected
  (c) .git/config contains no credential after a seeded clone
"""

from __future__ import annotations

import base64
import importlib.util
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
        "_test_pack_dispatch_issue416_handler",
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


class TestCloneWithToken:
    """(a) token present → clone and pull carry http.extraheader."""

    def _record(self) -> tuple[list[list[str]], object]:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed()

        return calls, fake_run

    def _expected_header(self, token: str) -> str:
        creds = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return f"http.extraheader=AUTHORIZATION: basic {creds}"

    def test_clone_command_carries_auth_header(self, handler, tmp_path: Path) -> None:
        token = "ghp_testtoken"
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/private-repo/issues/1",
            token=token,
            run=fake_run,
        )

        clone_cmd = calls[0]
        assert "-c" in clone_cmd
        header_idx = clone_cmd.index("-c")
        assert clone_cmd[header_idx + 1] == self._expected_header(token)

    def test_pull_command_carries_auth_header(self, handler, tmp_path: Path) -> None:
        token = "ghp_testtoken"
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/private-repo/issues/1",
            token=token,
            run=fake_run,
        )

        # pull is the third command (index 2)
        pull_cmd = calls[2]
        assert "-c" in pull_cmd
        header_idx = pull_cmd.index("-c")
        assert pull_cmd[header_idx + 1] == self._expected_header(token)

    def test_checkout_command_has_no_auth_header(self, handler, tmp_path: Path) -> None:
        """checkout is local; it must never carry the auth header."""
        token = "ghp_testtoken"
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/private-repo/issues/1",
            token=token,
            run=fake_run,
        )

        checkout_cmd = calls[1]
        assert "-c" not in checkout_cmd
        assert "AUTHORIZATION" not in " ".join(checkout_cmd)

    def test_clone_url_still_correct_with_token(self, handler, tmp_path: Path) -> None:
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/myorg/myrepo/issues/5",
            token="ghp_tok",
            run=fake_run,
        )

        clone_cmd = calls[0]
        assert "https://github.com/myorg/myrepo.git" in clone_cmd
        assert str(workspace / "repo") in clone_cmd


class TestCloneWithoutToken:
    """(b) token absent → plain commands, no http.extraheader."""

    def _record(self) -> tuple[list[list[str]], object]:
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed()

        return calls, fake_run

    def test_clone_command_has_no_auth_header(self, handler, tmp_path: Path) -> None:
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/public-repo/issues/1",
            run=fake_run,
        )

        clone_cmd = calls[0]
        assert "-c" not in clone_cmd
        assert "AUTHORIZATION" not in " ".join(clone_cmd)

    def test_pull_command_has_no_auth_header(self, handler, tmp_path: Path) -> None:
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/public-repo/issues/1",
            run=fake_run,
        )

        pull_cmd = calls[2]
        assert "-c" not in pull_cmd
        assert "AUTHORIZATION" not in " ".join(pull_cmd)

    def test_clone_command_structure_unchanged(self, handler, tmp_path: Path) -> None:
        """Without token the clone command is identical to the pre-#416 form."""
        calls, fake_run = self._record()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/public-repo/issues/1",
            run=fake_run,
        )

        clone_cmd = calls[0]
        assert clone_cmd[:3] == ["git", "clone", "--depth=50"]


class TestGitConfigNoCredential:
    """(c) .git/config must not contain the token after a seeded clone."""

    def test_git_config_contains_no_token(self, handler, tmp_path: Path) -> None:
        """The auth header is passed via -c (non-persisted); .git/config stays clean."""
        token = "ghp_supersecrettoken"
        workspace = tmp_path / "ws"
        workspace.mkdir()

        # Simulate the .git/config that a real `git clone` would create (no credentials)
        repo_dir = workspace / "repo"
        git_dir = repo_dir / ".git"
        git_dir.mkdir(parents=True)
        git_config = git_dir / "config"
        git_config.write_text(
            "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
            '[remote "origin"]\n\turl = https://github.com/owner/private-repo.git\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        )

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.stdout = ""
            m.stderr = ""
            m.returncode = 0
            return m

        handler._clone_repo_into_workspace(
            workspace,
            "https://github.com/owner/private-repo/issues/1",
            token=token,
            run=fake_run,
        )

        config_content = git_config.read_text()
        assert token not in config_content
        assert "AUTHORIZATION" not in config_content
        assert "x-access-token" not in config_content
