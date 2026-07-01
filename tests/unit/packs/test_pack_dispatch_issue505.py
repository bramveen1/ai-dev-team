"""Regression tests for #505: dispatch-id-matched slot release.

The recycled-index over-release race:
  1. Dispatch A acquires slot-N, babysit finishes, releases slot-N (first release).
  2. Dispatch B acquires the now-free slot-N.
  3. A stale second release fires for A (SIGTERM handler in babysit or the
     inline-mode backstop in handler).
  4. With the old _release_slot(slot_idx), step 3 deletes B's slot lock,
     breaking the POOL_SIZE cap (over-subscription / OOM class #470).
  5. With _release_slot_for_dispatch(dispatch_id), step 3 is a no-op because
     slot-N no longer matches A's dispatch_id.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test505_handler", PACK_DIR / "handler.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_babysit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test505_babysit", PACK_DIR / "babysit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── handler._release_slot_for_dispatch ───────────────────────────────────────


class TestHandlerReleaseForDispatch:
    def test_stale_release_is_noop_when_slot_recycled(self, tmp_path: Path) -> None:
        """Core #505 regression: releasing A's slot after B reacquired it is a no-op."""
        handler = _load_handler()
        slots_dir = handler._slots_dir(tmp_path)

        dispatch_a = "dispatch-A-505-handler"
        dispatch_b = "dispatch-B-505-handler"

        # A acquires slot-0.
        idx = handler._try_acquire_slot(slots_dir, dispatch_a)
        assert idx == 0

        # A's babysit finishes — owner-matched first release.
        handler._release_slot_for_dispatch(slots_dir, dispatch_a)
        assert not (slots_dir / "slot-0").exists()

        # B reacquires the recycled slot-0.
        idx_b = handler._try_acquire_slot(slots_dir, dispatch_b)
        assert idx_b == 0
        assert (slots_dir / "slot-0").read_text().strip() == dispatch_b

        # Stale second release fires for A (inline-mode handler backstop).
        handler._release_slot_for_dispatch(slots_dir, dispatch_a)

        # B's slot must survive — pool still counts 1 active worker.
        assert (slots_dir / "slot-0").exists(), "Stale release must not delete dispatch B's slot"
        assert (slots_dir / "slot-0").read_text().strip() == dispatch_b
        assert handler._count_active_slots(slots_dir) == 1

    def test_release_removes_own_slot(self, tmp_path: Path) -> None:
        handler = _load_handler()
        slots_dir = handler._slots_dir(tmp_path)

        dispatch_id = "dispatch-own-505"
        idx = handler._try_acquire_slot(slots_dir, dispatch_id)
        assert idx is not None

        handler._release_slot_for_dispatch(slots_dir, dispatch_id)
        assert not (slots_dir / f"slot-{idx}").exists()

    def test_release_is_idempotent_for_same_owner(self, tmp_path: Path) -> None:
        handler = _load_handler()
        slots_dir = handler._slots_dir(tmp_path)

        dispatch_id = "dispatch-idem-505"
        handler._try_acquire_slot(slots_dir, dispatch_id)

        handler._release_slot_for_dispatch(slots_dir, dispatch_id)
        handler._release_slot_for_dispatch(slots_dir, dispatch_id)  # no error, no-op

    def test_release_noop_when_no_slots_dir(self, tmp_path: Path) -> None:
        handler = _load_handler()
        slots_dir = tmp_path / handler.POOL_SLOTS_DIR_NAME
        assert not slots_dir.exists()

        handler._release_slot_for_dispatch(slots_dir, "dispatch-noslotsdir-505")  # no error


# ── babysit._release_slot_for_dispatch ───────────────────────────────────────


class TestBabysitReleaseForDispatch:
    def test_stale_release_is_noop_when_slot_recycled(self, tmp_path: Path, monkeypatch) -> None:
        """Core #505 regression in babysit: SIGTERM handler's release is a no-op after recycle."""
        babysit = _load_babysit(tmp_path, monkeypatch)

        with mock.patch.object(babysit, "_root", return_value=tmp_path):
            slots_dir = tmp_path / babysit.POOL_SLOTS_DIR_NAME
            slots_dir.mkdir()

            dispatch_a = "dispatch-A-505-babysit"
            dispatch_b = "dispatch-B-505-babysit"

            # A owns slot-0.
            slot_path = slots_dir / "slot-0"
            slot_path.write_text(dispatch_a)

            # A's finally releases it first.
            babysit._release_slot_for_dispatch(dispatch_a)
            assert not slot_path.exists()

            # B acquires the recycled slot-0.
            slot_path.write_text(dispatch_b)

            # Stale SIGTERM handler release fires for A.
            babysit._release_slot_for_dispatch(dispatch_a)

            # B's slot must survive.
            assert slot_path.exists(), "Stale release must not delete dispatch B's slot"
            assert slot_path.read_text().strip() == dispatch_b

    def test_release_removes_own_slot(self, tmp_path: Path, monkeypatch) -> None:
        babysit = _load_babysit(tmp_path, monkeypatch)

        with mock.patch.object(babysit, "_root", return_value=tmp_path):
            slots_dir = tmp_path / babysit.POOL_SLOTS_DIR_NAME
            slots_dir.mkdir()
            dispatch_id = "dispatch-own-babysit-505"
            (slots_dir / "slot-0").write_text(dispatch_id)

            babysit._release_slot_for_dispatch(dispatch_id)
            assert not (slots_dir / "slot-0").exists()

    def test_release_is_idempotent_for_same_owner(self, tmp_path: Path, monkeypatch) -> None:
        babysit = _load_babysit(tmp_path, monkeypatch)

        with mock.patch.object(babysit, "_root", return_value=tmp_path):
            slots_dir = tmp_path / babysit.POOL_SLOTS_DIR_NAME
            slots_dir.mkdir()
            dispatch_id = "dispatch-idem-babysit-505"
            (slots_dir / "slot-0").write_text(dispatch_id)

            babysit._release_slot_for_dispatch(dispatch_id)
            babysit._release_slot_for_dispatch(dispatch_id)  # no error, no-op

    def test_release_noop_when_no_slots_dir(self, tmp_path: Path, monkeypatch) -> None:
        babysit = _load_babysit(tmp_path, monkeypatch)

        with mock.patch.object(babysit, "_root", return_value=tmp_path):
            babysit._release_slot_for_dispatch("dispatch-noslotsdir-babysit-505")  # no error


# ── End-to-end: inline mode second release ───────────────────────────────────


class TestInlineModeRecycledSlot:
    """Simulate the full inline-mode race: babysit exits, slot recycled, handler backstop fires."""

    def test_pool_cap_holds_after_inline_recycled_release(self, tmp_path: Path) -> None:
        handler = _load_handler()
        slots_dir = handler._slots_dir(tmp_path)

        dispatch_a = "dispatch-A-inline-505"
        dispatch_b = "dispatch-B-inline-505"

        # Fill remaining slots with dummies so pool is nearly at capacity.
        for i in range(1, handler.POOL_SIZE):
            handler._try_acquire_slot(slots_dir, f"dispatch-dummy-{i}-505")

        # A takes the last slot.
        idx_a = handler._try_acquire_slot(slots_dir, dispatch_a)
        assert idx_a is not None
        assert handler._count_active_slots(slots_dir) == handler.POOL_SIZE

        # A's babysit exits — first owner-matched release.
        handler._release_slot_for_dispatch(slots_dir, dispatch_a)
        assert handler._count_active_slots(slots_dir) == handler.POOL_SIZE - 1

        # B grabs the freed slot (recycled index).
        idx_b = handler._try_acquire_slot(slots_dir, dispatch_b)
        assert idx_b is not None
        assert handler._count_active_slots(slots_dir) == handler.POOL_SIZE

        # Inline handler's stale second release fires for A.
        handler._release_slot_for_dispatch(slots_dir, dispatch_a)

        # Pool must still show POOL_SIZE active slots — no over-release.
        assert handler._count_active_slots(slots_dir) == handler.POOL_SIZE, (
            "Stale inline release must not decrement pool count below POOL_SIZE"
        )
