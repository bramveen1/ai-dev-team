"""Unit tests for router.atomic_io.

Covers ``atomic_write_text``'s fd-leak-on-chmod-failure regression (issue
#760) and ``atomic_read_json`` (issue #762): missing file, invalid JSON,
non-dict root, and the happy path, across both ``on_error`` modes.
"""

from __future__ import annotations

import os

import pytest

from router import atomic_io

pytestmark = pytest.mark.unit


def _open_fd_count() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def test_atomic_write_text_success(tmp_path):
    dest = tmp_path / "out.txt"
    atomic_io.atomic_write_text(dest, "hello")
    assert dest.read_text() == "hello"


def test_atomic_write_text_chmod_failure_closes_fd_and_unlinks_temp(tmp_path, monkeypatch):
    dest = tmp_path / "out.txt"
    dest.write_text("original")

    def _boom(*_args, **_kwargs):
        raise PermissionError("simulated chmod failure")

    monkeypatch.setattr(os, "chmod", _boom)

    before = _open_fd_count()
    for _ in range(20):
        with pytest.raises(PermissionError):
            atomic_io.atomic_write_text(dest, "new contents")
    after = _open_fd_count()

    assert after == before, "mkstemp fd leaked across repeated chmod failures"
    assert dest.read_text() == "original"
    assert list(tmp_path.iterdir()) == [dest]


def test_atomic_write_text_fdopen_failure_closes_fd_and_unlinks_temp(tmp_path, monkeypatch):
    dest = tmp_path / "out.txt"
    dest.write_text("original")

    real_fdopen = os.fdopen

    def _boom(fd, *args, **kwargs):
        os.close(fd)
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(os, "fdopen", _boom)

    before = _open_fd_count()
    for _ in range(20):
        with pytest.raises(OSError):
            atomic_io.atomic_write_text(dest, "new contents")
    after = _open_fd_count()

    monkeypatch.setattr(os, "fdopen", real_fdopen)

    assert after == before, "mkstemp fd leaked across repeated fdopen failures"
    assert dest.read_text() == "original"
    assert list(tmp_path.iterdir()) == [dest]


class TestAtomicReadJson:
    def test_happy_path(self, tmp_path):
        path = tmp_path / "data.json"
        path.write_text('{"a": 1}')
        assert atomic_io.atomic_read_json(path) == {"a": 1}

    def test_missing_file_returns_default_return_default_mode(self, tmp_path):
        path = tmp_path / "missing.json"
        assert atomic_io.atomic_read_json(path, default={"x": 1}) == {"x": 1}

    def test_missing_file_returns_default_raise_mode(self, tmp_path):
        """A missing file is the normal first-run case, not a corruption —
        it returns *default* even when on_error='raise'."""
        path = tmp_path / "missing.json"
        assert atomic_io.atomic_read_json(path, default={"x": 1}, on_error="raise") == {"x": 1}

    def test_invalid_json_returns_default(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        assert atomic_io.atomic_read_json(path, default={"fallback": True}) == {"fallback": True}

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        with pytest.raises(ValueError, match="not valid JSON"):
            atomic_io.atomic_read_json(path, on_error="raise")

    def test_non_dict_root_returns_default(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text('["a", "b"]')
        assert atomic_io.atomic_read_json(path, default={}) == {}

    def test_non_dict_root_raises(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text('["a", "b"]')
        with pytest.raises(ValueError, match="must be a JSON object"):
            atomic_io.atomic_read_json(path, on_error="raise")
