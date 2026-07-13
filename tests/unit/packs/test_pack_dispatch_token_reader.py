"""Unit tests for packs/dispatch/token_reader.py (#718).

Covers ``read_token_file``'s comment-skip / empty / missing / unreadable
semantics for both the ``raise_on_empty=False`` (dispatch/pr_review) and
``raise_on_empty=True`` (merge/``read_pat``) contracts, plus the
``skip_comments=False`` legacy-``read_pat`` behaviour this module replaces.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_token_reader():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test_token_reader", PACK_DIR / "token_reader.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def token_reader():
    return _load_token_reader()


class TestReadTokenFileNoneMode:
    """``raise_on_empty=False`` — the dispatch/pr_review contract."""

    def test_reads_valid_token(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("ghp_validtoken123\n")
        assert token_reader.read_token_file(token_file) == "ghp_validtoken123"

    def test_skips_comment_and_blank_lines(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("# placeholder header\n\n# another comment\nghp_actualtoken\n")
        assert token_reader.read_token_file(token_file) == "ghp_actualtoken"

    def test_comment_only_file_returns_none(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("# REPLACE_WITH_REAL_TOKEN\n")
        assert token_reader.read_token_file(token_file) is None

    def test_blank_file_returns_none(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("   \n\n")
        assert token_reader.read_token_file(token_file) is None

    def test_missing_file_returns_none(self, token_reader, tmp_path: Path) -> None:
        assert token_reader.read_token_file(tmp_path / "nonexistent") is None

    def test_permission_denied_returns_none(self, token_reader, tmp_path: Path) -> None:
        if os.getuid() == 0:
            pytest.skip("cannot simulate PermissionError as root")
        token_file = tmp_path / "restricted"
        token_file.write_text("ghp_secret")
        token_file.chmod(0o000)
        try:
            assert token_reader.read_token_file(token_file) is None
        finally:
            token_file.chmod(0o644)


class TestReadTokenFileRaiseMode:
    """``raise_on_empty=True`` — the merge/``read_pat`` contract."""

    def test_reads_valid_token(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("ghp_validtoken123\n")
        assert token_reader.read_token_file(token_file, raise_on_empty=True) == "ghp_validtoken123"

    def test_valid_token_with_leading_comment_header(self, token_reader, tmp_path: Path) -> None:
        """The #718 leak: read_pat used to return the comment text itself."""
        token_file = tmp_path / "token"
        token_file.write_text("# gh-aidt-merge PAT — do not commit\nghp_realtoken987\n")
        result = token_reader.read_token_file(token_file, raise_on_empty=True)
        assert result == "ghp_realtoken987"
        assert "#" not in result

    def test_comment_only_file_raises(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("# REPLACE_WITH_REAL_TOKEN\n")
        with pytest.raises(token_reader.TokenError, match="empty"):
            token_reader.read_token_file(token_file, raise_on_empty=True)

    def test_blank_file_raises(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("   \n")
        with pytest.raises(token_reader.TokenError, match="empty"):
            token_reader.read_token_file(token_file, raise_on_empty=True)

    def test_missing_file_raises(self, token_reader, tmp_path: Path) -> None:
        with pytest.raises(token_reader.TokenError, match="not found"):
            token_reader.read_token_file(tmp_path / "nonexistent", raise_on_empty=True)

    def test_permission_denied_raises(self, token_reader, tmp_path: Path) -> None:
        if os.getuid() == 0:
            pytest.skip("cannot simulate PermissionError as root")
        token_file = tmp_path / "restricted"
        token_file.write_text("ghp_secret")
        token_file.chmod(0o000)
        try:
            with pytest.raises(token_reader.TokenError, match="Cannot read"):
                token_reader.read_token_file(token_file, raise_on_empty=True)
        finally:
            token_file.chmod(0o644)


class TestSkipCommentsFalse:
    """``skip_comments=False`` — whole-file-as-token, the pre-#718 read_pat shape."""

    def test_returns_whole_stripped_file(self, token_reader, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("  ghp_wholefile  \n")
        assert token_reader.read_token_file(token_file, skip_comments=False) == "ghp_wholefile"

    def test_comment_header_leaks_into_token(self, token_reader, tmp_path: Path) -> None:
        """Demonstrates why read_pat now defaults to skip_comments=True."""
        token_file = tmp_path / "token"
        token_file.write_text("# a comment header\nghp_realtoken\n")
        result = token_reader.read_token_file(token_file, skip_comments=False)
        assert "# a comment header" in result
