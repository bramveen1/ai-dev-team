"""Unit tests for issue #431: _try_acquire_slot must not leak a slot file on os.write failure.

If os.write raises OSError (e.g. ENOSPC/EIO) after the slot file has been
created with O_CREAT|O_EXCL, the function must unlink the orphan file before
propagating the error.  Without the fix the pool shrinks permanently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_issue431_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def handler():
    return _load_handler()


class TestTryAcquireSlotWriteFailure:
    """_try_acquire_slot cleans up the slot file when os.write raises OSError."""

    def test_no_orphan_slot_file_after_os_write_error(self, handler, tmp_path: Path) -> None:
        slots_dir = tmp_path / ".slots"
        slots_dir.mkdir()

        dispatch_id = "dispatch-20260617T000000-test431"

        with patch("os.write", side_effect=OSError("ENOSPC")):
            with pytest.raises(OSError):
                handler._try_acquire_slot(slots_dir, dispatch_id)

        orphans = list(slots_dir.iterdir())
        assert orphans == [], f"Slot file(s) leaked after os.write failure: {orphans}"

    def test_oserror_is_propagated(self, handler, tmp_path: Path) -> None:
        """The OSError must not be swallowed — callers need to know the write failed."""
        slots_dir = tmp_path / ".slots"
        slots_dir.mkdir()

        dispatch_id = "dispatch-20260617T000000-test431b"

        with patch("os.write", side_effect=OSError("EIO")):
            with pytest.raises(OSError):
                handler._try_acquire_slot(slots_dir, dispatch_id)

    def test_happy_path_still_works(self, handler, tmp_path: Path) -> None:
        """Sanity: a successful acquire still returns a valid slot index."""
        slots_dir = tmp_path / ".slots"
        slots_dir.mkdir()

        dispatch_id = "dispatch-20260617T000000-test431c"

        result = handler._try_acquire_slot(slots_dir, dispatch_id)

        assert result == 0
        slot_file = slots_dir / "slot-0"
        assert slot_file.exists()
        assert slot_file.read_text() == dispatch_id
