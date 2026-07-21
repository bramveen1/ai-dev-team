"""Unit tests for router.atomic_io (issue #760: mkstemp fd leak on chmod failure).

Covers the failure path where something between ``mkstemp`` and ``os.fdopen``
raises: the fd must still be closed (no descriptor leak), the temp file must
be unlinked, any pre-existing target must stay intact, and the exception must
propagate.
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
