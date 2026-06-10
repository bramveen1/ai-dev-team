"""Make the pack's own directory importable as ``helpers.*`` and ``handler``."""

from __future__ import annotations

import sys
from pathlib import Path

_PACK_DIR = Path(__file__).resolve().parents[1]
if str(_PACK_DIR) not in sys.path:
    sys.path.insert(0, str(_PACK_DIR))
