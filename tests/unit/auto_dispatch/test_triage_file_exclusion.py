"""Unit tests for triage.py blast-radius rules after the file-count gate removal.

File *count* is no longer a gate (Bram, 2026-07-20): the merge bar is a Sam
review + green CI regardless of blast radius. What remains is the deny-list,
which runs over **every** changed file — tests included.
"""

from __future__ import annotations

import pytest

from router.auto_dispatch.triage import triage

pytestmark = pytest.mark.unit


class TestTriageDenyListOverAllFiles:
    def test_test_file_touching_deny_list_still_holds(self):
        # The deny-list runs over test files too — a test named after an auth
        # flow still trips the auth deny glob.
        decision, reason = triage(["tests/unit/test_auth_flow.py"])
        assert decision == "hold"
        assert reason == "auth"

    def test_many_files_including_tests_pass_when_clean(self):
        decision, reason = triage(
            [
                "router/auto_dispatch/triage.py",
                "router/auto_dispatch/config.py",
                "tests/unit/auto_dispatch/test_triage_file_exclusion.py",
                "tests/unit/test_a.py",
                "tests/unit/test_b.py",
            ]
        )
        assert decision == "low_risk"
        assert reason == "clean"

    def test_all_test_files_pass(self):
        decision, reason = triage(
            [
                "tests/unit/test_a.py",
                "tests/unit/test_b.py",
                "tests/unit/test_c.py",
            ]
        )
        assert decision == "low_risk"
        assert reason == "clean"
