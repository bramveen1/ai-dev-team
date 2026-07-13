"""Unit tests for triage.py's test-file exclusion from the multi-file gate (#726).

Only *non-test* files count against ``multi_file_threshold`` — a 1-code +
N-tests PR can take the auto path after approval. The deny-list still runs
over every changed file, tests included.
"""

from __future__ import annotations

import pytest

from router.auto_dispatch.triage import _is_test_file, triage

pytestmark = pytest.mark.unit


class TestIsTestFile:
    """Conservative allow-list classifier — anything ambiguous is "code"."""

    def test_under_tests_dir(self):
        assert _is_test_file("tests/unit/test_foo.py") is True

    def test_nested_under_tests_dir_non_test_named_file(self):
        assert _is_test_file("tests/fixtures/sample_payload.json") is True

    def test_test_prefix_filename_outside_tests_dir(self):
        assert _is_test_file("router/test_thing.py") is True

    def test_test_suffix_filename(self):
        assert _is_test_file("router/thing_test.py") is True

    def test_conftest_at_root(self):
        assert _is_test_file("conftest.py") is True

    def test_conftest_nested(self):
        assert _is_test_file("router/packs/conftest.py") is True

    def test_ordinary_code_file_is_not_test(self):
        assert _is_test_file("router/auto_dispatch/triage.py") is False

    def test_test_substring_in_filename_is_not_test(self):
        # "latest_thing.py" contains "test" as a substring but isn't a test file
        # by the allow-list rules — bias-to-hold treats it as code.
        assert _is_test_file("router/latest_thing.py") is False


class TestTriageTestFileExclusion:
    def test_one_code_file_plus_n_tests_passes(self):
        decision, reason = triage(
            [
                "router/auto_dispatch/triage.py",
                "tests/unit/auto_dispatch/test_triage_file_exclusion.py",
                "tests/unit/auto_dispatch/test_config_precedence.py",
            ],
            multi_file_threshold=1,
        )
        assert decision == "low_risk"
        assert reason == "clean"

    def test_two_code_files_still_holds(self):
        decision, reason = triage(
            [
                "router/auto_dispatch/triage.py",
                "router/auto_dispatch/config.py",
                "tests/unit/auto_dispatch/test_triage_file_exclusion.py",
            ],
            multi_file_threshold=1,
        )
        assert decision == "hold"
        assert reason == "multi_file:2"

    def test_ambiguous_file_counts_as_code(self):
        # "helpers.py" isn't under tests/ and doesn't match the test naming
        # patterns, so it counts as code and pushes the diff over threshold.
        decision, reason = triage(
            ["router/auto_dispatch/triage.py", "router/auto_dispatch/helpers.py"],
            multi_file_threshold=1,
        )
        assert decision == "hold"
        assert reason == "multi_file:2"

    def test_test_file_touching_deny_list_still_holds(self):
        decision, reason = triage(
            ["tests/unit/test_auth_flow.py"],
            multi_file_threshold=1,
        )
        assert decision == "hold"
        assert reason == "auth"

    def test_all_test_files_pass_with_zero_code_files(self):
        decision, reason = triage(
            [
                "tests/unit/test_a.py",
                "tests/unit/test_b.py",
                "tests/unit/test_c.py",
            ],
            multi_file_threshold=1,
        )
        assert decision == "low_risk"
        assert reason == "clean"
