"""Unit tests for router.epic.state — dispatched-tracker sidecar (#755)."""

from __future__ import annotations

import pytest

from router.epic.state import (
    _mark_dispatched,
    _read_dispatched,
    _remove_dispatched,
    _state_path,
)

pytestmark = pytest.mark.unit


class TestDispatchedTracker:
    def test_missing_file_reads_empty(self, tmp_path):
        assert _read_dispatched(str(tmp_path / "nope.json")) == {}

    def test_mark_then_read(self, tmp_path):
        path = str(tmp_path / "state.json")
        _mark_dispatched(path, 101, "auto-feature-orchestrator", 123.0)
        data = _read_dispatched(path)
        assert data == {"101": {"slug": "auto-feature-orchestrator", "ts": 123.0}}

    def test_mark_multiple_and_remove_one(self, tmp_path):
        path = str(tmp_path / "state.json")
        _mark_dispatched(path, 101, "slug-a", 1.0)
        _mark_dispatched(path, 102, "slug-a", 2.0)
        _remove_dispatched(path, 101)
        data = _read_dispatched(path)
        assert "101" not in data
        assert "102" in data

    def test_remove_missing_is_noop(self, tmp_path):
        path = str(tmp_path / "state.json")
        _remove_dispatched(path, 999)  # must not raise
        assert _read_dispatched(path) == {}

    def test_corrupt_file_reads_empty(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json")
        assert _read_dispatched(str(path)) == {}

    def test_non_dict_payload_reads_empty(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]")
        assert _read_dispatched(str(path)) == {}

    def test_state_path_explicit_override(self):
        assert _state_path({"state_path": "/custom/path.json"}) == "/custom/path.json"

    def test_state_path_defaults_when_unset(self):
        from router.epic.config import DEFAULT_STATE_PATH

        assert _state_path({}) == DEFAULT_STATE_PATH
