"""Regression tests for issue #798 — approval gate preview drops staged params.

``_evaluate_approval_gate()`` must carry the caller's staged ``budget_seconds``
and ``persona`` into the returned preview dict (which becomes ``draft.payload``
when the gate fires), otherwise the approval-execute path re-dispatches at the
handler's defaults regardless of what the operator staged.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test_gate_preview_handler", PACK_DIR / "handler.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


def test_gate_preview_carries_budget_seconds(handler, tmp_path: Path) -> None:
    preview = handler._evaluate_approval_gate(
        issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
        model="opus",
        root=tmp_path,
        now=datetime.now(timezone.utc),
        approval_cfg={"require_always": True, "destructive_keywords": []},
        cost_threshold=15.0,
        budget_seconds=5400,
        persona="review",
    )

    assert preview is not None
    assert preview["budget_seconds"] == 5400
    assert preview["persona"] == "review"


def test_gate_preview_omits_budget_seconds_when_not_supplied(handler, tmp_path: Path) -> None:
    """Legacy call sites that don't pass budget_seconds/persona see neither key — no KeyError downstream."""
    preview = handler._evaluate_approval_gate(
        issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
        model="opus",
        root=tmp_path,
        now=datetime.now(timezone.utc),
        approval_cfg={"require_always": True, "destructive_keywords": []},
        cost_threshold=15.0,
    )

    assert preview is not None
    assert "budget_seconds" not in preview
    assert "persona" not in preview


def test_dispatch_issue_gate_preview_end_to_end(handler, tmp_path: Path) -> None:
    """dispatch_issue() -> gate fires -> preview carries the caller's staged budget/persona."""
    result = handler.dispatch_issue(
        issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
        channel="C1",
        thread_ts="1.0",
        agent="sam",
        persona="review",
        budget_seconds=5400,
        workspace_root=tmp_path,
        _approval_cfg={"require_always": True, "destructive_keywords": []},
    )

    assert result["status"] == "approval_required"
    assert result["preview"]["budget_seconds"] == 5400
    assert result["preview"]["persona"] == "review"
