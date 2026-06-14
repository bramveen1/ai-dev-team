"""Pack approval-contract tests (issue #209).

Invariant: any handler function that can return ``{"status":
"approval_required", ...}`` MUST have its function name listed in the
``approve`` key of the corresponding ``pack.yaml``.  Without this
entry the router renders a Discard-only card and the dispatch can never
be approved.

Approach: AST-based scan of each pack's ``handler.py`` to find top-level
function definitions that contain a return statement returning a dict
literal with a ``"status"`` key whose value is the string
``"approval_required"``.  The function name is then compared against the
``approve`` list in ``pack.yaml``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKS_DIR = REPO_ROOT / "packs"


def _find_approval_required_functions(handler_path: Path) -> set[str]:
    """Return names of public top-level functions that can return approval_required.

    Only top-level (module-scope) function definitions with public names
    (not prefixed with ``_``) are considered.  Private helpers that build
    the approval_required dict are implementation details and are not verbs.

    A function is flagged when any ``Return`` node inside it yields a
    ``Dict`` literal whose ``"status"`` key has the literal value
    ``"approval_required"``.
    """
    tree = ast.parse(handler_path.read_text())
    approving: set[str] = set()
    # Only inspect top-level (module-body) function definitions.
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_"):
            # Private helpers are not verbs; skip.
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or child.value is None:
                continue
            if not isinstance(child.value, ast.Dict):
                continue
            for key, val in zip(child.value.keys, child.value.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "status"
                    and isinstance(val, ast.Constant)
                    and val.value == "approval_required"
                ):
                    approving.add(node.name)
                    break
    return approving


def _load_pack_approve(pack_dir: Path) -> list[str]:
    """Return the ``approve`` list from ``pack.yaml``, or ``[]`` if absent."""
    yaml_path = pack_dir / "pack.yaml"
    if not yaml_path.exists():
        return []
    data = yaml.safe_load(yaml_path.read_text()) or {}
    return list(data.get("approve") or [])


# ── Dispatch pack contract (explicit, so the failure message is obvious) ──────


class TestDispatchPackApprovalContract:
    """The dispatch pack's approval_required returns are all in pack.yaml approve."""

    def test_dispatch_issue_in_approve_list(self) -> None:
        """dispatch_issue returns approval_required → must appear in pack.yaml approve."""
        pack_dir = PACKS_DIR / "dispatch"
        handler_path = pack_dir / "handler.py"

        approving_fns = _find_approval_required_functions(handler_path)
        assert "dispatch_issue" in approving_fns, (
            "dispatch_issue must contain a return with status=approval_required "
            "(AST scan found no such return — has the function been renamed?)"
        )

        approve_list = _load_pack_approve(pack_dir)
        assert "dispatch_issue" in approve_list, (
            "dispatch_issue returns approval_required but is not listed in "
            f"packs/dispatch/pack.yaml approve (currently: {approve_list!r})"
        )

    def test_no_unlisted_approval_required_returns(self) -> None:
        """Every function returning approval_required is in pack.yaml approve."""
        pack_dir = PACKS_DIR / "dispatch"
        handler_path = pack_dir / "handler.py"

        approving_fns = _find_approval_required_functions(handler_path)
        approve_list = _load_pack_approve(pack_dir)

        unlisted = sorted(approving_fns - set(approve_list))
        assert not unlisted, (
            f"Functions returning approval_required but missing from pack.yaml approve: {unlisted!r}\n"
            f"Current approve list: {approve_list!r}"
        )


# ── Full-fleet scan: all packs with handlers ──────────────────────────────────


class TestAllPacksApprovalContract:
    """No pack may silently omit an approval_required return from its approve list."""

    def test_all_packs_with_handlers_honor_contract(self) -> None:
        """Scan every pack/*/handler.py; collect and assert zero violations."""
        violations: list[str] = []

        for pack_dir in sorted(PACKS_DIR.iterdir()):
            if not pack_dir.is_dir() or pack_dir.name.startswith("_"):
                continue
            handler_path = pack_dir / "handler.py"
            if not handler_path.exists():
                continue

            approving_fns = _find_approval_required_functions(handler_path)
            if not approving_fns:
                continue

            approve_list = _load_pack_approve(pack_dir)
            for fn_name in sorted(approving_fns):
                if fn_name not in approve_list:
                    violations.append(
                        f"{pack_dir.name}/handler.py: {fn_name!r} returns "
                        f"approval_required but is absent from pack.yaml approve "
                        f"(currently: {approve_list!r})"
                    )

        assert not violations, "Approval contract violations found:\n" + "\n".join(f"  • {v}" for v in violations)

    def test_ast_scanner_finds_known_approval_return(self) -> None:
        """Smoke-test the AST scanner against the dispatch handler's known return."""
        handler_path = PACKS_DIR / "dispatch" / "handler.py"
        found = _find_approval_required_functions(handler_path)
        assert found, "AST scanner returned no results for packs/dispatch/handler.py — the scanner logic may be broken"
