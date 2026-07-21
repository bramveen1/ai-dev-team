"""Persisted dispatch tracker for the epic orchestrator loop (#755).

One small JSON sidecar (atomic writes, mirrors ``router.auto_dispatch.state``):
child issue numbers we've already posted an approval-card draft for, so a
later tick doesn't re-post the same card while the human decision is still
pending. Keyed by issue number → ``{"slug": ..., "ts": ...}``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from router.atomic_io import atomic_write_json
from router.epic.config import DEFAULT_STATE_PATH

logger = logging.getLogger(__name__)


def _state_path(payload: dict) -> str:
    """Resolve the sidecar path: explicit payload override, else the default."""
    return payload.get("state_path") or DEFAULT_STATE_PATH


def _read_dispatched(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_dispatched(path: str, data: dict) -> None:
    try:
        atomic_write_json(path, data, sort_keys=True)
    except OSError as exc:
        logger.warning("epic_orchestrator: failed to write dispatched tracker to %s: %s", path, exc)


def _mark_dispatched(path: str, issue_num: int, slug: str, now_ts: float) -> None:
    data = _read_dispatched(path)
    data[str(issue_num)] = {"slug": slug, "ts": now_ts}
    _write_dispatched(path, data)


def _remove_dispatched(path: str, issue_num: int) -> None:
    data = _read_dispatched(path)
    if data.pop(str(issue_num), None) is not None:
        _write_dispatched(path, data)
