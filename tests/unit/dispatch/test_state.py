"""Unit tests for router.dispatch.state — sidecar state-file helpers."""

from __future__ import annotations

import os

import pytest

from router.dispatch import state as dstate

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    return str(tmp_path)


class TestDispatchRoot:
    def test_explicit_override_wins(self, monkeypatch, root):
        monkeypatch.setenv(dstate.DISPATCH_ROOT_ENV, "/tmp/from-env")
        assert dstate.dispatch_root(root) == dstate.Path(root)

    def test_env_var_then_default(self, monkeypatch):
        monkeypatch.setenv(dstate.DISPATCH_ROOT_ENV, "/tmp/from-env")
        assert str(dstate.dispatch_root(None)) == "/tmp/from-env"
        monkeypatch.delenv(dstate.DISPATCH_ROOT_ENV, raising=False)
        assert str(dstate.dispatch_root(None)) == dstate.DEFAULT_DISPATCH_ROOT


class TestWriteAndReadField:
    def test_write_creates_dir_and_file(self, root):
        path = dstate.write_field("disp-1", "pid", "1234", root=root)
        assert path.exists()
        assert path.read_text() == "1234"

    def test_write_is_atomic_no_tmp_left_behind(self, root):
        dstate.write_field("disp-1", "pid", "1234", root=root)
        d = dstate.dispatch_dir("disp-1", root=root)
        # No `.pid.tmp` lingering after the rename.
        assert {p.name for p in d.iterdir()} == {"pid"}

    def test_read_missing_returns_none(self, root):
        assert dstate.read_field("disp-missing", "pid", root=root) is None

    def test_read_strips_whitespace(self, root):
        dstate.write_field("disp-1", "cost", "  0.12\n", root=root)
        assert dstate.read_field("disp-1", "cost", root=root) == "0.12"

    def test_write_overwrites(self, root):
        dstate.write_field("disp-1", "last_event", "tool_use", root=root)
        dstate.write_field("disp-1", "last_event", "result", root=root)
        assert dstate.read_field("disp-1", "last_event", root=root) == "result"


class TestReadState:
    def test_only_present_fields_included(self, root):
        dstate.write_field("d1", dstate.FIELD_PID, "111", root=root)
        dstate.write_field("d1", dstate.FIELD_AGENT, "sam", root=root)

        state = dstate.read_state("d1", root=root)
        assert state == {dstate.FIELD_PID: "111", dstate.FIELD_AGENT: "sam"}

    def test_missing_dispatch_returns_empty(self, root):
        assert dstate.read_state("never-existed", root=root) == {}


class TestListDispatchIds:
    def test_empty_when_root_missing(self, tmp_path):
        assert dstate.list_dispatch_ids(root=str(tmp_path / "absent")) == []

    def test_lists_subdirs_alphabetically(self, root):
        for d_id in ("disp-b", "disp-a", "disp-c"):
            dstate.write_field(d_id, dstate.FIELD_PID, "1", root=root)
        assert dstate.list_dispatch_ids(root=root) == ["disp-a", "disp-b", "disp-c"]

    def test_skips_hidden_files(self, root):
        dstate.write_field("disp-a", dstate.FIELD_PID, "1", root=root)
        # Drop a stray hidden file at the root (e.g. the `.health` marker
        # from the dispatch_health probe).
        from pathlib import Path

        (Path(root) / ".health").write_text("ok")
        assert dstate.list_dispatch_ids(root=root) == ["disp-a"]


class TestHeartbeatAlive:
    def test_fresh_heartbeat_is_alive(self, root):
        dstate.write_field("d1", dstate.FIELD_PID, "1", root=root)
        hb = dstate.dispatch_dir("d1", root=root) / dstate.FIELD_HEARTBEAT
        hb.touch()
        assert dstate.heartbeat_alive("d1", root=root) is True

    def test_absent_heartbeat_is_not_alive(self, root):
        dstate.write_field("d1", dstate.FIELD_PID, "1", root=root)
        # No heartbeat file written.
        assert dstate.heartbeat_alive("d1", root=root) is False

    def test_missing_dispatch_dir_is_not_alive(self, root):
        assert dstate.heartbeat_alive("never-exists", root=root) is False

    def test_stale_heartbeat_is_not_alive(self, root):
        import time as _time

        dstate.write_field("d1", dstate.FIELD_PID, "1", root=root)
        hb = dstate.dispatch_dir("d1", root=root) / dstate.FIELD_HEARTBEAT
        hb.touch()
        # Back-date the mtime by 200 s — well past the 45 s stale threshold.
        old = _time.time() - 200
        os.utime(hb, (old, old))
        assert dstate.heartbeat_alive("d1", root=root) is False

    def test_custom_max_age_respected(self, root):
        import time as _time

        dstate.write_field("d1", dstate.FIELD_PID, "1", root=root)
        hb = dstate.dispatch_dir("d1", root=root) / dstate.FIELD_HEARTBEAT
        hb.touch()
        # Back-date by 50 s: stale for max_age=30, alive for max_age=120.
        mid = _time.time() - 50
        os.utime(hb, (mid, mid))
        assert dstate.heartbeat_alive("d1", root=root, max_age_seconds=30) is False
        assert dstate.heartbeat_alive("d1", root=root, max_age_seconds=120) is True
