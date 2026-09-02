"""Unit tests for router.auto_dispatch.circuit_breaker (#868).

Covers the primitives directly: signed-out detection, trip/clear state
transitions, and idempotency (a second ``trip`` while already tripped must
not re-write state or report a fresh trip — that's what lets callers
dedupe the one-shot Slack notice).
"""

from __future__ import annotations

import pytest

from router.auto_dispatch.circuit_breaker import _breaker_path, clear, is_tripped, looks_signed_out, trip

pytestmark = pytest.mark.unit


class TestLooksSignedOut:
    @pytest.mark.parametrize(
        "text",
        [
            "Not logged in · Please run /login",
            "not logged in",
            "PLEASE RUN /LOGIN",
            "some preamble\nNot logged in\nsome trailer",
        ],
    )
    def test_detects_signed_out_markers(self, text):
        assert looks_signed_out(text) is True

    @pytest.mark.parametrize("text", ["", None, "launched", '{"status": "launched"}'])
    def test_no_false_positive_on_unrelated_text(self, text):
        assert looks_signed_out(text) is False


class TestTripAndClear:
    @pytest.fixture
    def breaker_path(self, tmp_path):
        return str(tmp_path / "_auto_dispatch_circuit_breaker.json")

    def test_not_tripped_initially(self, breaker_path):
        assert is_tripped(breaker_path) is None

    def test_trip_persists_state_and_reports_first_trip(self, breaker_path):
        first = trip(breaker_path, reason="signed_out", now_ts=1000.0)
        assert first is True
        state = is_tripped(breaker_path)
        assert state is not None
        assert state["reason"] == "signed_out"
        assert state["tripped_ts"] == 1000.0

    def test_redundant_trip_is_a_no_op(self, breaker_path):
        trip(breaker_path, reason="signed_out", now_ts=1000.0)
        second = trip(breaker_path, reason="signed_out", now_ts=2000.0)
        assert second is False
        # State is untouched by the redundant call — still the first trip's timestamp.
        assert is_tripped(breaker_path)["tripped_ts"] == 1000.0

    def test_clear_resets_tripped_state_and_reports_it_was_tripped(self, breaker_path):
        trip(breaker_path, reason="signed_out", now_ts=1000.0)
        was_tripped = clear(breaker_path, cleared_by="bram")
        assert was_tripped is True
        assert is_tripped(breaker_path) is None

    def test_clear_on_untripped_breaker_is_a_no_op(self, breaker_path):
        was_tripped = clear(breaker_path)
        assert was_tripped is False
        assert is_tripped(breaker_path) is None

    def test_trip_after_clear_works_again(self, breaker_path):
        trip(breaker_path, reason="signed_out", now_ts=1000.0)
        clear(breaker_path)
        third = trip(breaker_path, reason="signed_out", now_ts=3000.0)
        assert third is True
        assert is_tripped(breaker_path)["tripped_ts"] == 3000.0


class TestBreakerPath:
    def test_defaults_beside_counter_path(self):
        path = _breaker_path({"counter_path": "/var/lib/dispatch/_auto_dispatch_counters.json"})
        assert path == "/var/lib/dispatch/_auto_dispatch_circuit_breaker.json"

    def test_explicit_override_wins(self):
        path = _breaker_path({"circuit_breaker_path": "/tmp/custom_breaker.json"})
        assert path == "/tmp/custom_breaker.json"
