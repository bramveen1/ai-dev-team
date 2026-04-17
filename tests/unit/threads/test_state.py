"""Tests for router.threads.state — SQLite-backed thread state store."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from router.threads.state import (
    ThreadState,
    ThreadStateStore,
    get_default_store,
    reset_default_store,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    s = ThreadStateStore(str(tmp_path / "thread_state.db"))
    yield s
    s.close()


class TestThreadStateStore:
    def test_get_missing_returns_none(self, store):
        assert store.get("C001", "1.0") is None
        assert store.get_active_agent("C001", "1.0") is None

    def test_set_and_get_active_agent(self, store):
        now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        state = store.set_active_agent("C001", "1.0", "lisa", mentioned=True, now=now)

        assert isinstance(state, ThreadState)
        assert state.active_agent == "lisa"
        assert state.last_mention_at == now
        assert state.updated_at == now

        assert store.get_active_agent("C001", "1.0") == "lisa"
        roundtrip = store.get("C001", "1.0")
        assert roundtrip.active_agent == "lisa"
        assert roundtrip.last_mention_at == now

    def test_upsert_replaces_active_agent(self, store):
        t1 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 17, 12, 5, 0, tzinfo=timezone.utc)

        store.set_active_agent("C001", "1.0", "lisa", mentioned=True, now=t1)
        store.set_active_agent("C001", "1.0", "sam", mentioned=True, now=t2)

        assert store.get_active_agent("C001", "1.0") == "sam"
        state = store.get("C001", "1.0")
        assert state.last_mention_at == t2
        assert state.updated_at == t2

    def test_unmentioned_update_preserves_last_mention_at(self, store):
        t1 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 17, 12, 5, 0, tzinfo=timezone.utc)

        store.set_active_agent("C001", "1.0", "lisa", mentioned=True, now=t1)
        store.set_active_agent("C001", "1.0", "lisa", mentioned=False, now=t2)

        state = store.get("C001", "1.0")
        assert state.last_mention_at == t1  # unchanged
        assert state.updated_at == t2  # bumped

    def test_different_threads_are_isolated(self, store):
        store.set_active_agent("C001", "1.0", "lisa", mentioned=True)
        store.set_active_agent("C002", "2.0", "sam", mentioned=True)

        assert store.get_active_agent("C001", "1.0") == "lisa"
        assert store.get_active_agent("C002", "2.0") == "sam"

    def test_clear_removes_row(self, store):
        store.set_active_agent("C001", "1.0", "lisa", mentioned=True)
        assert store.clear("C001", "1.0") is True
        assert store.get("C001", "1.0") is None
        # Clearing again is a no-op and returns False
        assert store.clear("C001", "1.0") is False

    def test_persists_across_store_instances(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        s1 = ThreadStateStore(db_path)
        s1.set_active_agent("C001", "1.0", "lisa", mentioned=True)
        s1.close()

        s2 = ThreadStateStore(db_path)
        try:
            assert s2.get_active_agent("C001", "1.0") == "lisa"
        finally:
            s2.close()


class TestDefaultStore:
    def test_default_store_is_singleton(self, tmp_path, monkeypatch):
        import router.threads.state as state_mod

        monkeypatch.setattr(state_mod, "DEFAULT_DB_PATH", str(tmp_path / "s.db"))
        reset_default_store()

        s1 = get_default_store()
        s2 = get_default_store()
        assert s1 is s2

        reset_default_store()
        s3 = get_default_store()
        assert s3 is not s1

        reset_default_store()
