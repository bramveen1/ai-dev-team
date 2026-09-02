"""Regression guard for #871: the /var/lib/dispatch reclaim chown must not gate boot.

#871 added a recursive `chown -R ... /var/lib/dispatch` to router/entrypoint.sh
to reclaim root-owned orphan subtrees. Run *synchronously* it walks every inode
in the (potentially multi-GB / hundreds-of-thousands-of-inodes) dispatch tree
before the final `exec gosu claude`, which pushed router readiness past the
deploy daemon's health-check window and boot-looped cold restarts.

The fix backgrounds it (`&`) so it can never block the `exec`. These tests pin
that invariant against the real entrypoint and prove the ordering behaviourally.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_ENTRYPOINT = REPO_ROOT / "router" / "entrypoint.sh"


def _reclaim_line(body: str) -> str:
    """Return the recursive reclaim chown line targeting /var/lib/dispatch."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("chown -R") and "/var/lib/dispatch" in stripped and not stripped.startswith("#"):
            return stripped
    raise AssertionError("recursive reclaim chown for /var/lib/dispatch not found")


class TestReclaimChownIsBackgrounded:
    def test_reclaim_chown_is_detached(self):
        line = _reclaim_line(ROUTER_ENTRYPOINT.read_text())
        assert line.endswith("&"), (
            "the recursive reclaim chown must be backgrounded (&) so a large "
            f"dispatch tree can never gate router boot; got: {line!r}"
        )

    def test_reclaim_chown_is_not_synchronous(self):
        """The old synchronous `|| true` form must be gone (it blocked exec)."""
        line = _reclaim_line(ROUTER_ENTRYPOINT.read_text())
        assert not line.endswith("|| true"), "reclaim chown must be detached with &, not run synchronously"

    def test_reclaim_precedes_exec_gosu(self):
        """Sanity: the reclaim still runs before the final privilege-drop exec."""
        body = ROUTER_ENTRYPOINT.read_text()
        # locate the *recursive dispatch* reclaim specifically
        m = re.search(r"chown -R[^\n]*/var/lib/dispatch[^\n]*", body)
        assert m is not None
        exec_idx = body.index("exec gosu claude")
        assert m.start() < exec_idx, "reclaim must be launched before exec gosu"


class TestBackgroundedCommandDoesNotBlock:
    """Behavioural proof that a backgrounded chown-shaped command doesn't gate exec."""

    def test_exec_line_reached_before_background_completes(self):
        # Mirror the entrypoint construct: a slow best-effort task detached with
        # `&`, followed by the line that must not wait for it.
        snippet = """
set -e
slow_start=$SECONDS
( sleep 3 ) 2>/dev/null &
echo "REACHED_EXEC=$((SECONDS - slow_start))"
"""
        result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "REACHED_EXEC=0" in result.stdout, (
            f"the line after a backgrounded task must run immediately, not wait for it; stdout={result.stdout!r}"
        )
