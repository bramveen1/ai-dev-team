"""Unit tests for router.auto_dispatch — autonomous bug-backlog loop (#535).

Focus areas:
- Triage gate: each deny-list category routes to 'hold'.
- Multi-file threshold routes to 'hold'.
- Clean single-file low-risk diff routes to 'low_risk'.
- Ambiguous/unknown path → 'hold' (false-negative bias).
- Counter persistence across restarts (daily/hourly).
- Eligibility selector: AC block detection.
- Config loading defaults and overrides.
- tick() gate conditions: disabled, daily cap, hourly rate, in-flight.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from router.auto_dispatch import (
    _has_ac_block,
    _pre_dispatch_triage,
    get_counters,
    handle_pr_verdict,
    increment_counters,
    load_auto_dispatch_config,
    tick,
    triage,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def now():
    return datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def now_ts(now):
    return now.timestamp()


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture
def counter_file(tmp_path):
    return str(tmp_path / "counters.json")


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "dispatch.yaml"
    path.write_text(
        yaml.dump(
            {
                "auto_dispatch": {
                    "enabled": True,
                    "rate_per_hour": 2,
                    "daily_cap": 6,
                    "shadow_mode": True,
                    "multi_file_threshold": 1,
                }
            }
        )
    )
    return str(path)


# ---------------------------------------------------------------------------
# Triage gate — deny-list categories
# ---------------------------------------------------------------------------


class TestTriageDenyList:
    """Each deny-list category and the multi-file threshold routes to 'hold'."""

    def test_auth_path_holds(self):
        decision, reason = triage(["src/auth/login.py"])
        assert decision == "hold"
        assert reason == "auth"

    def test_auth_authentication_path_holds(self):
        decision, reason = triage(["router/authentication/middleware.py"])
        assert decision == "hold"
        assert reason == "auth"

    def test_auth_filename_holds(self):
        decision, reason = triage(["router/user_auth.py"])
        assert decision == "hold"
        assert reason == "auth"

    def test_billing_path_holds(self):
        decision, reason = triage(["src/billing/invoices.py"])
        assert decision == "hold"
        assert reason == "billing"

    def test_payment_path_holds(self):
        decision, reason = triage(["src/payment/processor.py"])
        assert decision == "hold"
        assert reason == "billing"

    def test_invoice_path_holds(self):
        decision, reason = triage(["api/invoice/generate.py"])
        assert decision == "hold"
        assert reason == "billing"

    def test_billing_filename_holds(self):
        decision, reason = triage(["router/stripe_billing.py"])
        assert decision == "hold"
        assert reason == "billing"

    def test_db_migration_sql_holds(self):
        decision, reason = triage(["db/create_tables.sql"])
        assert decision == "hold"
        assert reason == "db_migration"

    def test_db_migration_path_holds(self):
        decision, reason = triage(["alembic/versions/001_add_users.py"])
        assert decision == "hold"
        assert reason == "db_migration"

    def test_db_migration_filename_holds(self):
        decision, reason = triage(["router/migrate_sessions.py"])
        assert decision == "hold"
        assert reason == "db_migration"

    def test_docker_compose_holds(self):
        decision, reason = triage(["docker-compose.yml"])
        assert decision == "hold"
        assert reason == "deploy_config"

    def test_dockerfile_holds(self):
        decision, reason = triage(["router/Dockerfile"])
        assert decision == "hold"
        assert reason == "deploy_config"

    def test_systemd_path_holds(self):
        decision, reason = triage(["systemd/router.service"])
        assert decision == "hold"
        assert reason == "deploy_config"

    def test_github_actions_holds(self):
        decision, reason = triage([".github/workflows/ci.yml"])
        assert decision == "hold"
        assert reason == "deploy_config"

    def test_deploy_path_holds(self):
        decision, reason = triage(["scripts/deploy/rollout.sh"])
        assert decision == "hold"
        assert reason == "deploy_config"

    def test_secrets_path_holds(self):
        decision, reason = triage(["config/secrets/api_key.txt"])
        assert decision == "hold"
        assert reason == "secrets"

    def test_env_file_holds(self):
        decision, reason = triage([".env.production"])
        assert decision == "hold"
        assert reason == "secrets"

    def test_secret_filename_holds(self):
        decision, reason = triage(["router/secret_manager.py"])
        assert decision == "hold"
        assert reason == "secrets"

    def test_token_filename_holds(self):
        decision, reason = triage(["router/token_refresh.py"])
        assert decision == "hold"
        assert reason == "secrets"


class TestTriageMultiFile:
    """Multi-file threshold routes to 'hold'."""

    def test_single_file_at_threshold_passes(self):
        decision, _ = triage(["router/slack_format.py"], multi_file_threshold=1)
        assert decision == "low_risk"

    def test_two_files_above_threshold_holds(self):
        decision, reason = triage(["router/a.py", "router/b.py"], multi_file_threshold=1)
        assert decision == "hold"
        assert "multi_file" in reason

    def test_multi_file_reason_contains_count(self):
        _, reason = triage(["a.py", "b.py", "c.py"], multi_file_threshold=1)
        assert "3" in reason

    def test_threshold_two_allows_two_files(self):
        decision, _ = triage(["router/a.py", "router/b.py"], multi_file_threshold=2)
        assert decision == "low_risk"

    def test_threshold_two_holds_on_three_files(self):
        decision, reason = triage(["router/a.py", "router/b.py", "router/c.py"], multi_file_threshold=2)
        assert decision == "hold"
        assert "multi_file" in reason


class TestTriageLowRisk:
    """A clean single-file low-risk diff routes to 'low_risk'."""

    def test_single_clean_python_file_low_risk(self):
        decision, reason = triage(["router/slack_format.py"])
        assert decision == "low_risk"
        assert reason == "clean"

    def test_single_test_file_low_risk(self):
        decision, reason = triage(["tests/unit/test_slack_format.py"])
        assert decision == "low_risk"

    def test_single_readme_low_risk(self):
        decision, reason = triage(["README.md"])
        assert decision == "low_risk"

    def test_single_requirements_file_low_risk(self):
        decision, reason = triage(["router/requirements.txt"])
        assert decision == "low_risk"


class TestTriageUnknownAmbiguous:
    """Ambiguous / unknown path → 'hold', not 'low_risk' (false-negative bias)."""

    def test_empty_file_list_holds(self):
        decision, reason = triage([])
        assert decision == "hold"
        assert reason == "unknown_diff"

    def test_single_deny_in_mixed_list_holds(self):
        # Even if only one file out of two matches the deny-list, the decision is hold.
        decision, reason = triage(["router/slack_format.py", "router/auth_helper.py"], multi_file_threshold=10)
        assert decision == "hold"
        assert reason == "auth"


# ---------------------------------------------------------------------------
# AC block detection
# ---------------------------------------------------------------------------


class TestAcBlockDetection:
    def test_has_ac_block_returns_true(self):
        body = "## Summary\nFixes things.\n\n## Acceptance Criteria\n- [ ] works"
        assert _has_ac_block(body) is True

    def test_missing_ac_block_returns_false(self):
        body = "## Summary\nJust a description."
        assert _has_ac_block(body) is False

    def test_empty_body_returns_false(self):
        assert _has_ac_block("") is False

    def test_none_body_returns_false(self):
        assert _has_ac_block(None) is False  # type: ignore[arg-type]

    def test_ac_must_be_h2(self):
        body = "### Acceptance Criteria\n- works"
        assert _has_ac_block(body) is False

    def test_ac_mid_body_is_found(self):
        body = "some preamble\n\n## Acceptance Criteria\n- item 1"
        assert _has_ac_block(body) is True


# ---------------------------------------------------------------------------
# Counter persistence
# ---------------------------------------------------------------------------


class TestCounterPersistence:
    def test_fresh_counters_are_zero(self, counter_file, now_ts):
        c = get_counters(counter_file, now_ts)
        assert c["daily_count"] == 0
        assert c["hourly_count"] == 0

    def test_increment_bumps_both(self, counter_file, now_ts):
        increment_counters(counter_file, now_ts)
        c = get_counters(counter_file, now_ts)
        assert c["daily_count"] == 1
        assert c["hourly_count"] == 1

    def test_increment_twice(self, counter_file, now_ts):
        increment_counters(counter_file, now_ts)
        increment_counters(counter_file, now_ts)
        c = get_counters(counter_file, now_ts)
        assert c["daily_count"] == 2
        assert c["hourly_count"] == 2

    def test_daily_counter_resets_next_day(self, counter_file, now_ts):
        increment_counters(counter_file, now_ts)
        # Simulate next day: +26 hours
        next_day_ts = now_ts + 26 * 3600
        c = get_counters(counter_file, next_day_ts)
        assert c["daily_count"] == 0

    def test_hourly_counter_resets_next_hour(self, counter_file, now_ts):
        increment_counters(counter_file, now_ts)
        # Simulate next hour: +70 minutes
        next_hour_ts = now_ts + 70 * 60
        c = get_counters(counter_file, next_hour_ts)
        assert c["hourly_count"] == 0

    def test_counters_persist_across_reads(self, counter_file, now_ts):
        increment_counters(counter_file, now_ts)
        # Read back from disk — simulates a restart.
        c = get_counters(counter_file, now_ts)
        assert c["daily_count"] == 1

    def test_missing_file_returns_zeros(self, counter_file, now_ts):
        c = get_counters("/nonexistent/path/counters.json", now_ts)
        assert c["daily_count"] == 0
        assert c["hourly_count"] == 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadAutoDispatchConfig:
    def test_defaults_when_no_file(self):
        cfg = load_auto_dispatch_config("/nonexistent/path.yaml")
        assert cfg["enabled"] is False
        assert cfg["rate_per_hour"] == 2
        assert cfg["daily_cap"] == 6
        assert cfg["shadow_mode"] is True
        assert cfg["multi_file_threshold"] == 1

    def test_reads_enabled_from_file(self, tmp_path):
        p = tmp_path / "dispatch.yaml"
        p.write_text(yaml.dump({"auto_dispatch": {"enabled": True, "daily_cap": 10}}))
        cfg = load_auto_dispatch_config(str(p))
        assert cfg["enabled"] is True
        assert cfg["daily_cap"] == 10
        # Unset keys still get defaults.
        assert cfg["shadow_mode"] is True

    def test_no_auto_dispatch_block_uses_defaults(self, tmp_path):
        p = tmp_path / "dispatch.yaml"
        p.write_text(yaml.dump({"quota": {"threshold_usd": 50}}))
        cfg = load_auto_dispatch_config(str(p))
        assert cfg["enabled"] is False

    def test_invalid_yaml_returns_defaults(self, tmp_path):
        p = tmp_path / "dispatch.yaml"
        p.write_text("{{ invalid yaml :")
        cfg = load_auto_dispatch_config(str(p))
        assert cfg["enabled"] is False


# ---------------------------------------------------------------------------
# tick() gate conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTickGates:
    """tick() must bail early on each blocking condition and log it."""

    @pytest.fixture
    def base_payload(self, counter_file, config_file, tmp_path):
        return {
            "repo": "bramveen1/ai-dev-team",
            "pat_path": str(tmp_path / "fake.token"),
            "counter_path": counter_file,
            "config_path": config_file,
            "dispatch_timeout": 5,
        }

    @pytest.fixture
    def enabled_config(self, tmp_path):
        p = tmp_path / "dispatch_enabled.yaml"
        cfg = {
            "auto_dispatch": {
                "enabled": True,
                "rate_per_hour": 2,
                "daily_cap": 6,
                "shadow_mode": True,
                "multi_file_threshold": 1,
            }
        }
        p.write_text(yaml.dump(cfg))
        return str(p)

    async def test_disabled_returns_ok_skipped(self, slack_client, now, base_payload, tmp_path):
        # config_file has enabled=True but we override with a disabled config.
        disabled_cfg = tmp_path / "disabled.yaml"
        disabled_cfg.write_text(yaml.dump({"auto_dispatch": {"enabled": False}}))
        payload = {**base_payload, "config_path": str(disabled_cfg)}
        result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "disabled"

    async def test_daily_cap_blocks(self, slack_client, now, base_payload, counter_file, enabled_config):
        # Pre-fill daily counter to cap.
        payload = {**base_payload, "config_path": enabled_config}
        for _ in range(6):
            increment_counters(counter_file, now.timestamp())
        result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "daily_cap"

    async def test_hourly_rate_blocks(self, slack_client, now, base_payload, counter_file, enabled_config):
        # Pre-fill hourly counter to cap.
        payload = {**base_payload, "config_path": enabled_config}
        for _ in range(2):
            increment_counters(counter_file, now.timestamp())
        result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "hourly_rate"

    async def test_in_flight_dispatch_blocks(self, slack_client, now, base_payload, enabled_config):
        payload = {**base_payload, "config_path": enabled_config}
        with patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=True):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "in_flight"

    async def test_missing_repo_returns_ok_skipped(self, slack_client, now, base_payload):
        payload = {**base_payload, "repo": ""}
        result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "no_repo"

    async def test_open_prs_blocks(self, slack_client, now, base_payload, enabled_config, tmp_path):
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        payload = {**base_payload, "config_path": enabled_config, "pat_path": str(pat_file)}
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch(
                "router.auto_dispatch._get_open_dev_prs",
                new=AsyncMock(return_value=[{"number": 1}]),
            ),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert "open_prs" in result["skipped"]

    async def test_no_candidate_returns_skipped(self, slack_client, now, base_payload, enabled_config, tmp_path):
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        payload = {**base_payload, "config_path": enabled_config, "pat_path": str(pat_file)}
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_open_dev_prs", new=AsyncMock(return_value=[])),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=None)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "no_candidate"


# ---------------------------------------------------------------------------
# handle_pr_verdict — shadow mode and live mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandlePrVerdict:
    @pytest.fixture
    def pat_file(self, tmp_path):
        p = tmp_path / "merge.token"
        p.write_text("gh_merge_token")
        return str(p)

    async def test_shadow_mode_posts_would_merge(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="pass")),
            patch(
                "router.auto_dispatch._get_pr_details",
                new=AsyncMock(return_value={"head": {"sha": "abc123"}, "title": "Fix bug"}),
            ),
            patch("router.auto_dispatch._ci_green", new=AsyncMock(return_value=True)),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=True,
            )
        assert result["status"] == "would_merge"
        assert result["pr"] == 42
        slack_client.chat_postMessage.assert_called_once()
        call_text = slack_client.chat_postMessage.call_args.kwargs.get("text", "")
        assert "would auto-merge" in call_text

    async def test_triage_hold_returns_hold(self, slack_client, pat_file):
        with patch(
            "router.auto_dispatch._get_pr_files",
            new=AsyncMock(return_value=["config/secrets/key.txt"]),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=True,
            )
        assert result["status"] == "hold"
        assert result["reason"] == "secrets"

    async def test_verdict_fail_holds(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="fail")),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=True,
            )
        assert result["status"] == "hold"
        assert result["reason"] == "verdict_fail"

    async def test_ci_not_green_holds(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="pass")),
            patch(
                "router.auto_dispatch._get_pr_details",
                new=AsyncMock(return_value={"head": {"sha": "abc123"}, "title": "Fix"}),
            ),
            patch("router.auto_dispatch._ci_green", new=AsyncMock(return_value=False)),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=True,
            )
        assert result["status"] == "hold"
        assert result["reason"] == "ci_not_green"

    async def test_live_mode_merges_and_posts(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="pass")),
            patch(
                "router.auto_dispatch._get_pr_details",
                new=AsyncMock(return_value={"head": {"sha": "abc123"}, "title": "Fix bug", "state": "open"}),
            ),
            patch("router.auto_dispatch._ci_green", new=AsyncMock(return_value=True)),
            patch("router.auto_dispatch._squash_merge", new=AsyncMock(return_value=True)),
            patch("router.auto_dispatch._verify_merged", new=AsyncMock(return_value=True)),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=False,
            )
        assert result["status"] == "merged"
        assert result["pr"] == 42
        call_text = slack_client.chat_postMessage.call_args.kwargs.get("text", "")
        assert "merged by bot (auto-dispatch loop)" in call_text

    async def test_no_verdict_yet_returns_pending(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value=None)),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=True,
            )
        assert result["status"] == "pending"

    async def test_multi_file_pr_holds(self, slack_client, pat_file):
        with patch(
            "router.auto_dispatch._get_pr_files",
            new=AsyncMock(return_value=["router/a.py", "router/b.py"]),
        ):
            result = await handle_pr_verdict(
                repo="bramveen1/ai-dev-team",
                pr_num=42,
                issue_num=10,
                slack_client=slack_client,
                destination="C_NOTIFY",
                pat_path=pat_file,
                shadow_mode=True,
                multi_file_threshold=1,
            )
        assert result["status"] == "hold"
        assert "multi_file" in result["reason"]


# ---------------------------------------------------------------------------
# Pre-dispatch triage
# ---------------------------------------------------------------------------


class TestPreDispatchTriage:
    def test_clean_issue_passes(self):
        issue = {"title": "Fix null pointer in slack_format", "body": "## AC\n- works"}
        decision, _ = _pre_dispatch_triage(issue)
        assert decision == "low_risk"

    def test_auth_keyword_in_title_holds(self):
        issue = {"title": "Fix authentication token refresh bug", "body": ""}
        decision, reason = _pre_dispatch_triage(issue)
        assert decision == "hold"
        assert reason == "auth"

    def test_billing_keyword_holds(self):
        issue = {"title": "Fix billing calculation error", "body": ""}
        decision, reason = _pre_dispatch_triage(issue)
        assert decision == "hold"
        assert reason == "billing"

    def test_migration_keyword_holds(self):
        issue = {"title": "Fix migration script failure", "body": ""}
        decision, reason = _pre_dispatch_triage(issue)
        assert decision == "hold"
        assert reason == "db_migration"

    def test_secret_keyword_holds(self):
        issue = {"title": "Fix secret rotation race condition", "body": ""}
        decision, reason = _pre_dispatch_triage(issue)
        assert decision == "hold"
        assert reason == "secrets"
