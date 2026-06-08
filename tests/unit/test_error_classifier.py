"""Unit tests for router.error_classifier."""

import re

import pytest

from router.dispatcher import ApiError, DispatchError, DispatchTimeoutError
from router.error_classifier import build_error_message, classify_error, make_correlation_id

pytestmark = pytest.mark.unit


class TestMakeCorrelationId:
    def test_returns_eight_hex_chars(self):
        corr_id = make_correlation_id()
        assert len(corr_id) == 8
        assert re.fullmatch(r"[0-9a-f]{8}", corr_id), f"Not hex: {corr_id!r}"

    def test_unique_each_call(self):
        ids = {make_correlation_id() for _ in range(20)}
        assert len(ids) > 1


class TestClassifyError:
    def test_529_overload_is_api_overload(self):
        exc = ApiError(529, "overloaded")
        category, template = classify_error(exc)
        assert category == "ApiOverload"
        assert "retry" in template.lower()
        assert "{corr_id}" in template

    def test_429_rate_limit_is_api_overload(self):
        exc = ApiError(429, "rate limited")
        category, template = classify_error(exc)
        assert category == "ApiOverload"

    def test_401_auth_error_is_api_auth_quota(self):
        exc = ApiError(401, "unauthorized")
        category, template = classify_error(exc)
        assert category == "ApiAuthQuota"
        assert "{corr_id}" in template

    def test_timeout_category(self):
        exc = DispatchTimeoutError("timed out after 30s in container foo")
        category, template = classify_error(exc)
        assert category == "Timeout"
        assert "{corr_id}" in template

    def test_generic_dispatch_error_is_cli_error(self):
        exc = DispatchError("agent foo exited with code 1")
        category, template = classify_error(exc)
        assert category == "CliError"
        assert "{corr_id}" in template

    def test_generic_exception_is_router_exception(self):
        exc = ValueError("unexpected internal state")
        category, template = classify_error(exc)
        assert category == "RouterException"
        assert "{corr_id}" in template


class TestBuildErrorMessage:
    def test_corr_id_substituted_in_message(self):
        exc = ApiError(529, "overloaded")
        category, msg = build_error_message(exc, "deadbeef")
        assert "deadbeef" in msg
        assert "{corr_id}" not in msg

    def test_timeout_message_contains_corr_id(self):
        exc = DispatchTimeoutError("timed out")
        _, msg = build_error_message(exc, "cafebabe")
        assert "cafebabe" in msg

    def test_router_exception_message_contains_corr_id(self):
        exc = RuntimeError("boom")
        _, msg = build_error_message(exc, "12345678")
        assert "12345678" in msg

    def test_cli_error_message_contains_corr_id(self):
        exc = DispatchError("exit 1")
        _, msg = build_error_message(exc, "aabbccdd")
        assert "aabbccdd" in msg
