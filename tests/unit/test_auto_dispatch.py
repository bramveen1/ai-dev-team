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
    _add_awaiting,
    _apply_auto_merge_label,
    _awaiting_path,
    _dispatch_worker,
    _has_ac_block,
    _pre_dispatch_triage,
    _process_awaiting,
    _read_awaiting,
    _remove_awaiting,
    _TokenError,
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

    @pytest.fixture
    def live_config(self, tmp_path):
        """Config with shadow_mode=False for testing real dispatch path."""
        p = tmp_path / "dispatch_live.yaml"
        cfg = {
            "auto_dispatch": {
                "enabled": True,
                "rate_per_hour": 2,
                "daily_cap": 6,
                "shadow_mode": False,
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

    async def test_successful_dispatch_enrols_awaiting(self, slack_client, now, base_payload, live_config, tmp_path):
        """The core #535 fix: a successful dispatch MUST enrol the issue in the
        awaiting tracker, else the verdict/merge bridge never fires.
        Requires live mode (shadow_mode=False) — shadow mode never spawns a worker."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        awaiting_path = str(tmp_path / "awaiting.json")
        payload = {
            **base_payload,
            "config_path": live_config,
            "pat_path": str(pat_file),
            "awaiting_path": awaiting_path,
        }
        candidate = {
            "number": 77,
            "title": "Fix null deref in slack_format",
            "html_url": "https://github.com/bramveen1/ai-dev-team/issues/77",
            "body": "## Acceptance Criteria\n- works",
        }
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=candidate)),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            patch("router.auto_dispatch._slack_post_with_ts", new=AsyncMock(return_value="1234567890.000001")),
            patch("router.auto_dispatch._dispatch_worker", new=AsyncMock()) as worker,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "dispatched"
        assert result["issue"] == 77
        worker.assert_awaited_once()
        # The issue is now tracked for verdict+merge on a later tick.
        assert "77" in _read_awaiting(awaiting_path)

    async def test_awaiting_issue_excluded_from_candidate_pick(
        self, slack_client, now, base_payload, live_config, tmp_path
    ):
        """An issue already in the awaiting set must be excluded from the next
        candidate pick so it's never double-dispatched before its PR is seen."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        awaiting_path = str(tmp_path / "awaiting.json")
        _add_awaiting(awaiting_path, 77, now.timestamp())
        payload = {
            **base_payload,
            "config_path": live_config,
            "pat_path": str(pat_file),
            "awaiting_path": awaiting_path,
        }
        captured = {}

        async def _capture(repo, pat, *, in_flight_issue_nums):
            captured["nums"] = in_flight_issue_nums
            return None

        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            patch("router.auto_dispatch.pick_next_candidate", new=_capture),
        ):
            await tick(payload=payload, slack_client=slack_client, now=now)
        assert 77 in captured["nums"]

    async def test_tick_timeout_protects_scheduler(self, slack_client, now, base_payload, enabled_config):
        """#502: a slow tick must abort within its budget and return ok so the
        scheduler's inline await never freezes."""
        payload = {**base_payload, "config_path": enabled_config, "tick_timeout": 0.05}

        async def _hang(**_kwargs):
            import asyncio as _a

            await _a.sleep(5)

        with patch("router.auto_dispatch._tick_impl", new=_hang):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "tick_timeout"

    async def test_no_candidate_returns_skipped(self, slack_client, now, base_payload, enabled_config, tmp_path):
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        payload = {**base_payload, "config_path": enabled_config, "pat_path": str(pat_file)}
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=None)),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["skipped"] == "no_candidate"

    async def test_shadow_mode_blocks_real_dispatch(self, slack_client, now, base_payload, enabled_config, tmp_path):
        """#556: shadow_mode=True must prevent _dispatch_worker from being called.
        The tick returns would_dispatch with a ghost-line Slack post instead."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        payload = {**base_payload, "config_path": enabled_config, "pat_path": str(pat_file), "destination": "C_SHADOW"}
        candidate = {
            "number": 55,
            "title": "Fix crash in logger",
            "html_url": "https://github.com/bramveen1/ai-dev-team/issues/55",
            "body": "## Acceptance Criteria\n- works",
        }
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=candidate)),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            patch("router.auto_dispatch._dispatch_worker", new=AsyncMock()) as worker,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["action"] == "would_dispatch"
        assert result["issue"] == 55
        # Real worker must NOT be spawned in shadow mode.
        worker.assert_not_awaited()
        # A ghost-line notification must have been posted.
        slack_client.chat_postMessage.assert_called()
        ghost_text = slack_client.chat_postMessage.call_args.kwargs.get("text", "")
        assert "ghost" in ghost_text or ":ghost:" in ghost_text

    async def test_live_mode_passes_kickoff_ts_to_worker(self, slack_client, now, base_payload, live_config, tmp_path):
        """#556: live dispatch posts a kickoff message and threads the returned ts
        into _dispatch_worker so the handler receives a valid thread_ts."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        awaiting_path = str(tmp_path / "awaiting.json")
        payload = {
            **base_payload,
            "config_path": live_config,
            "pat_path": str(pat_file),
            "awaiting_path": awaiting_path,
        }
        candidate = {
            "number": 66,
            "title": "Fix index error",
            "html_url": "https://github.com/bramveen1/ai-dev-team/issues/66",
            "body": "## Acceptance Criteria\n- works",
        }
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=candidate)),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            patch(
                "router.auto_dispatch._slack_post_with_ts",
                new=AsyncMock(return_value="1111111111.000002"),
            ) as post_with_ts,
            patch("router.auto_dispatch._dispatch_worker", new=AsyncMock()) as worker,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "dispatched"
        # The kickoff-ts helper must have been called.
        post_with_ts.assert_awaited_once()
        # _dispatch_worker must receive the ts from the kickoff post.
        worker.assert_awaited_once()
        call_kwargs = worker.await_args.kwargs
        assert call_kwargs["thread_ts"] == "1111111111.000002"

    async def test_empty_kickoff_ts_skips_dispatch(self, slack_client, now, base_payload, live_config, tmp_path):
        """#563 negative path: when _slack_post_with_ts returns empty string
        (no channel or Slack failure), tick must bail out deterministically
        without calling _dispatch_worker and without crash-looping."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        awaiting_path = str(tmp_path / "awaiting.json")
        payload = {
            **base_payload,
            "config_path": live_config,
            "pat_path": str(pat_file),
            "awaiting_path": awaiting_path,
        }
        candidate = {
            "number": 77,
            "title": "Empty Slack context issue",
            "html_url": "https://github.com/bramveen1/ai-dev-team/issues/77",
            "body": "## Acceptance Criteria\n- works",
        }
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=candidate)),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            # Simulate Slack post failure → empty kickoff_ts.
            patch("router.auto_dispatch._slack_post_with_ts", new=AsyncMock(return_value="")),
            patch("router.auto_dispatch._dispatch_worker", new=AsyncMock()) as worker,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        # Must return ok/skipped, not crash.
        assert result["status"] == "ok"
        assert result.get("skipped") == "empty_slack_context"
        # Worker must NOT have been called.
        worker.assert_not_awaited()

    async def test_approval_required_does_not_enrol_awaiting(
        self, slack_client, now, base_payload, live_config, tmp_path
    ):
        """#563: when the worker returns approval_required the tick must NOT
        increment counters or enrol the issue in the awaiting tracker — no worker
        was spawned so there is no PR to watch for."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        awaiting_path = str(tmp_path / "awaiting.json")
        payload = {
            **base_payload,
            "config_path": live_config,
            "pat_path": str(pat_file),
            "awaiting_path": awaiting_path,
        }
        candidate = {
            "number": 88,
            "title": "Needs approval",
            "html_url": "https://github.com/bramveen1/ai-dev-team/issues/88",
            "body": "## Acceptance Criteria\n- works",
        }
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=candidate)),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            patch(
                "router.auto_dispatch._slack_post_with_ts",
                new=AsyncMock(return_value="2222222222.000001"),
            ),
            # Worker returns approval_required (gate fired).
            patch("router.auto_dispatch._dispatch_worker", new=AsyncMock(return_value="approval_required")),
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["status"] == "ok"
        assert result["action"] == "approval_required"
        assert result["issue"] == 88
        # Issue must NOT be in the awaiting tracker.
        import json as _json
        import pathlib

        awaiting_file = pathlib.Path(awaiting_path)
        if awaiting_file.exists():
            awaiting = _json.loads(awaiting_file.read_text())
            assert "88" not in awaiting

    async def test_dispatch_proceeds_with_open_dev_prs(self, slack_client, now, base_payload, live_config, tmp_path):
        """tick() must dispatch even when dev-worker PRs are open — suppression gate removed (#573)."""
        pat_file = tmp_path / "fake.token"
        pat_file.write_text("gh_test_token")
        awaiting_path = str(tmp_path / "awaiting.json")
        payload = {
            **base_payload,
            "config_path": live_config,
            "pat_path": str(pat_file),
            "awaiting_path": awaiting_path,
        }
        candidate = {
            "number": 42,
            "title": "Fix regression",
            "html_url": "https://github.com/bramveen1/ai-dev-team/issues/42",
            "body": "## Acceptance Criteria\n- works",
        }
        with (
            patch("router.auto_dispatch._has_any_in_flight_dispatch", return_value=False),
            patch("router.auto_dispatch._get_in_flight_issue_nums", return_value=set()),
            patch("router.auto_dispatch.pick_next_candidate", new=AsyncMock(return_value=candidate)),
            patch("router.auto_dispatch._process_awaiting", new=AsyncMock()),
            patch("router.auto_dispatch._slack_post_with_ts", new=AsyncMock(return_value="9999999999.000001")),
            patch("router.auto_dispatch._dispatch_worker", new=AsyncMock()) as worker,
        ):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result["action"] == "dispatched"
        assert result["issue"] == 42
        worker.assert_awaited_once()


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

    async def test_shadow_mode_posts_would_label(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="pass")),
            patch(
                "router.auto_dispatch._get_pr_details",
                new=AsyncMock(return_value={"head": {"sha": "abc123"}, "title": "Fix bug"}),
            ),
            patch("router.auto_dispatch._ci_green", new=AsyncMock(return_value=True)),
            # Shadow mode must NOT touch the PR — assert the label call never fires.
            patch("router.auto_dispatch._apply_auto_merge_label", new=AsyncMock(return_value=True)) as label,
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
        assert result["status"] == "would_label"
        assert result["pr"] == 42
        label.assert_not_called()
        slack_client.chat_postMessage.assert_called_once()
        call_text = slack_client.chat_postMessage.call_args.kwargs.get("text", "")
        assert "would label" in call_text

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

    async def test_live_mode_labels_and_posts(self, slack_client, pat_file):
        label = AsyncMock(return_value=True)
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="pass")),
            patch(
                "router.auto_dispatch._get_pr_details",
                new=AsyncMock(return_value={"head": {"sha": "abc123"}, "title": "Fix bug", "state": "open"}),
            ),
            patch("router.auto_dispatch._ci_green", new=AsyncMock(return_value=True)),
            patch("router.auto_dispatch._apply_auto_merge_label", new=label),
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
        assert result["status"] == "labeled"
        assert result["pr"] == 42
        # The loop hands off to the queue — it labels, it does not merge.
        label.assert_awaited_once()
        assert label.await_args.args[:2] == ("bramveen1/ai-dev-team", 42)
        call_text = slack_client.chat_postMessage.call_args.kwargs.get("text", "")
        assert "auto-merge" in call_text

    async def test_live_mode_label_failure_holds(self, slack_client, pat_file):
        with (
            patch("router.auto_dispatch._get_pr_files", new=AsyncMock(return_value=["router/fix.py"])),
            patch("router.auto_dispatch._get_verdict_from_pr", new=AsyncMock(return_value="pass")),
            patch(
                "router.auto_dispatch._get_pr_details",
                new=AsyncMock(return_value={"head": {"sha": "abc123"}, "title": "Fix bug", "state": "open"}),
            ),
            patch("router.auto_dispatch._ci_green", new=AsyncMock(return_value=True)),
            patch("router.auto_dispatch._apply_auto_merge_label", new=AsyncMock(return_value=False)),
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
        assert result["status"] == "hold"
        assert result["reason"] == "label_failed"

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


# ---------------------------------------------------------------------------
# Awaiting tracker — the bridge that drives dispatched issues to verdict+merge
# ---------------------------------------------------------------------------


class TestAwaitingTracker:
    """Round-trip persistence + corruption tolerance for the awaiting set."""

    def test_add_then_read(self, tmp_path):
        path = str(tmp_path / "awaiting.json")
        _add_awaiting(path, 42, 1000.0)
        assert _read_awaiting(path) == {"42": 1000.0}

    def test_add_multiple_and_remove_one(self, tmp_path):
        path = str(tmp_path / "awaiting.json")
        _add_awaiting(path, 42, 1000.0)
        _add_awaiting(path, 43, 1001.0)
        _remove_awaiting(path, 42)
        assert _read_awaiting(path) == {"43": 1001.0}

    def test_remove_missing_is_noop(self, tmp_path):
        path = str(tmp_path / "awaiting.json")
        _add_awaiting(path, 42, 1000.0)
        _remove_awaiting(path, 999)  # not present
        assert _read_awaiting(path) == {"42": 1000.0}

    def test_missing_file_reads_empty(self, tmp_path):
        assert _read_awaiting(str(tmp_path / "nope.json")) == {}

    def test_corrupt_file_reads_empty(self, tmp_path):
        path = tmp_path / "awaiting.json"
        path.write_text("{not valid json")
        assert _read_awaiting(str(path)) == {}

    def test_non_dict_payload_reads_empty(self, tmp_path):
        path = tmp_path / "awaiting.json"
        path.write_text("[1, 2, 3]")
        assert _read_awaiting(str(path)) == {}

    def test_awaiting_path_derives_from_counter_path(self):
        path = _awaiting_path({"counter_path": "/var/lib/dispatch/_counters.json"})
        assert path == "/var/lib/dispatch/_auto_dispatch_awaiting.json"

    def test_awaiting_path_explicit_override(self):
        path = _awaiting_path({"awaiting_path": "/tmp/custom.json"})
        assert path == "/tmp/custom.json"


@pytest.mark.asyncio
class TestProcessAwaiting:
    """_process_awaiting drives each tracked issue through handle_pr_verdict."""

    @pytest.fixture
    def cfg(self):
        return {"shadow_mode": False, "multi_file_threshold": 1}

    async def test_pending_keeps_entry(self, slack_client, tmp_path, cfg):
        awaiting_path = str(tmp_path / "awaiting.json")
        _add_awaiting(awaiting_path, 10, 1000.0)
        payload = {"awaiting_path": awaiting_path, "counter_path": str(tmp_path / "c.json")}
        with (
            patch("router.auto_dispatch._get_pr_for_issue", new=AsyncMock(return_value={"number": 42})),
            patch("router.auto_dispatch.handle_pr_verdict", new=AsyncMock(return_value={"status": "pending"})),
        ):
            await _process_awaiting(
                repo="r/r",
                pat="t",
                slack_client=slack_client,
                destination="C",
                cfg=cfg,
                payload=payload,
                now_ts=2000.0,
            )
        # Still pending → must remain in the tracker for the next tick.
        assert _read_awaiting(awaiting_path) == {"10": 1000.0}

    async def test_terminal_status_removes_entry(self, slack_client, tmp_path, cfg):
        awaiting_path = str(tmp_path / "awaiting.json")
        _add_awaiting(awaiting_path, 10, 1000.0)
        payload = {"awaiting_path": awaiting_path, "counter_path": str(tmp_path / "c.json")}
        with (
            patch("router.auto_dispatch._get_pr_for_issue", new=AsyncMock(return_value={"number": 42})),
            patch("router.auto_dispatch.handle_pr_verdict", new=AsyncMock(return_value={"status": "labeled"})),
        ):
            await _process_awaiting(
                repo="r/r",
                pat="t",
                slack_client=slack_client,
                destination="C",
                cfg=cfg,
                payload=payload,
                now_ts=2000.0,
            )
        assert _read_awaiting(awaiting_path) == {}

    async def test_no_pr_within_age_keeps_entry(self, slack_client, tmp_path, cfg):
        awaiting_path = str(tmp_path / "awaiting.json")
        _add_awaiting(awaiting_path, 10, 1000.0)
        payload = {"awaiting_path": awaiting_path, "counter_path": str(tmp_path / "c.json")}
        with patch("router.auto_dispatch._get_pr_for_issue", new=AsyncMock(return_value=None)):
            await _process_awaiting(
                repo="r/r",
                pat="t",
                slack_client=slack_client,
                destination="C",
                cfg=cfg,
                payload=payload,
                now_ts=1500.0,  # 500s < 24h
            )
        assert _read_awaiting(awaiting_path) == {"10": 1000.0}

    async def test_no_pr_past_max_age_expires_entry(self, slack_client, tmp_path, cfg):
        awaiting_path = str(tmp_path / "awaiting.json")
        _add_awaiting(awaiting_path, 10, 1000.0)
        payload = {"awaiting_path": awaiting_path, "counter_path": str(tmp_path / "c.json")}
        with patch("router.auto_dispatch._get_pr_for_issue", new=AsyncMock(return_value=None)):
            await _process_awaiting(
                repo="r/r",
                pat="t",
                slack_client=slack_client,
                destination="C",
                cfg=cfg,
                payload=payload,
                now_ts=1000.0 + 25 * 3600,
            )
        assert _read_awaiting(awaiting_path) == {}

    async def test_corrupt_key_is_dropped(self, slack_client, tmp_path, cfg):
        awaiting_path = tmp_path / "awaiting.json"
        awaiting_path.write_text('{"not-an-int": 1000.0}')
        payload = {"awaiting_path": str(awaiting_path), "counter_path": str(tmp_path / "c.json")}
        with patch("router.auto_dispatch._get_pr_for_issue", new=AsyncMock()) as m:
            await _process_awaiting(
                repo="r/r",
                pat="t",
                slack_client=slack_client,
                destination="C",
                cfg=cfg,
                payload=payload,
                now_ts=2000.0,
            )
        m.assert_not_called()  # corrupt key dropped before any PR lookup
        assert _read_awaiting(str(awaiting_path)) == {}


# ---------------------------------------------------------------------------
# _apply_auto_merge_label — hands the PR to the single merger (merge_queue.py)
# ---------------------------------------------------------------------------


def _resp(status_code):
    r = MagicMock()
    r.status_code = status_code
    return r


@pytest.mark.asyncio
class TestApplyAutoMergeLabel:
    async def test_posts_auto_merge_label(self):
        post = AsyncMock(return_value=_resp(200))
        with patch("router.auto_dispatch._gh_post", new=post):
            ok = await _apply_auto_merge_label("r/r", 5, "pat")
        assert ok is True
        # _gh_post(path, pat, body) — labels endpoint + the auto-merge label.
        assert post.await_args.args[0] == "/repos/r/r/issues/5/labels"
        body = post.await_args.args[2]
        assert body == {"labels": ["auto-merge"]}

    async def test_201_created_is_success(self):
        with patch("router.auto_dispatch._gh_post", new=AsyncMock(return_value=_resp(201))):
            ok = await _apply_auto_merge_label("r/r", 5, "pat")
        assert ok is True

    async def test_non_2xx_returns_false(self):
        with patch("router.auto_dispatch._gh_post", new=AsyncMock(return_value=_resp(422))):
            ok = await _apply_auto_merge_label("r/r", 5, "pat")
        assert ok is False

    async def test_401_raises_token_error(self):
        with patch("router.auto_dispatch._gh_post", new=AsyncMock(return_value=_resp(401))):
            with pytest.raises(_TokenError):
                await _apply_auto_merge_label("r/r", 5, "pat")


# ---------------------------------------------------------------------------
# _dispatch_worker — real docker-exec launch path (mirrors the approval
# execute path: --approved bypasses the human gate, poll returns 'launched')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDispatchWorker:
    def _patches(self, run_return):
        extras = MagicMock()
        extras.env = {"WORKERS_BOT_TOKEN": "xoxb-test"}
        return (
            patch("router.config.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
            patch("router.dispatcher._run_in_container", new=AsyncMock(return_value=run_return)),
            patch("router.packs.dispatch_hook.pack_cli_extras", return_value=extras),
        )

    async def test_launches_without_approved_flag_with_poll(self, slack_client):
        """#563: the auto path must NOT pass --approved; the handler evaluates
        the gate normally.  --supervision-mode poll and pack env still applied."""
        run = AsyncMock(return_value=('{"status": "launched", "dispatch_id": "d1", "pid": 9}', "", 0))
        gam, _, pce = self._patches(None)
        with gam, patch("router.dispatcher._run_in_container", new=run), pce:
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=slack_client,
                destination="C123",
                payload={"worker_agent": "sam"},
            )
        run.assert_awaited_once()
        cmd = run.await_args.kwargs["command"]
        # #563: --approved must NOT be in the command
        assert "--approved" not in cmd
        assert "poll" in cmd  # --supervision-mode poll
        assert "dispatch_issue" in cmd
        # pack env is injected so the #257 workers_token_missing guard stays quiet
        assert run.await_args.kwargs["env"] == {"WORKERS_BOT_TOKEN": "xoxb-test"}
        assert result == "launched"

    async def test_approval_required_calls_create_draft_fn(self, slack_client):
        """#563: when the handler returns approval_required the auto path must
        call _create_draft_fn (not raise) so a Slack approval card is posted."""
        run = AsyncMock(
            return_value=(
                '{"status": "approval_required", "draft_id": "abc123", '
                '"preview": {"repo": "o/r", "issue_url": "https://github.com/o/r/issues/42", '
                '"model": "sonnet", "gate_reason": "always"}}',
                "",
                0,
            )
        )
        create_draft = AsyncMock(return_value=None)
        gam, _, pce = self._patches(None)
        with gam, patch("router.dispatcher._run_in_container", new=run), pce:
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=slack_client,
                destination="C123",
                payload={"worker_agent": "sam"},
                _create_draft_fn=create_draft,
            )
        # Must NOT raise and must return approval_required.
        assert result == "approval_required"
        create_draft.assert_awaited_once()
        # Draft creation must receive the parsed preview and issue info.
        call_kw = create_draft.await_args.kwargs
        assert call_kw["agent_name"] == "sam"
        assert call_kw["issue_url"] == "https://github.com/o/r/issues/42"
        assert call_kw["issue_num"] == 42
        assert call_kw["gate_preview"]["gate_reason"] == "always"

    async def test_approval_required_does_not_raise(self, slack_client):
        """#563: approval_required must be a clean return, not an exception."""
        run = AsyncMock(
            return_value=(
                '{"status": "approval_required", "draft_id": "xyz", "preview": {}}',
                "",
                0,
            )
        )
        create_draft = AsyncMock(return_value=None)
        gam, _, pce = self._patches(None)
        with gam, patch("router.dispatcher._run_in_container", new=run), pce:
            # Must not raise.
            result = await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=slack_client,
                destination="C123",
                payload={"worker_agent": "sam"},
                _create_draft_fn=create_draft,
            )
        assert result == "approval_required"

    async def test_non_launched_status_raises(self, slack_client):
        run = AsyncMock(return_value=('{"status": "gate_blocked", "reason": "denied"}', "", 0))
        gam, _, pce = self._patches(None)
        with gam, patch("router.dispatcher._run_in_container", new=run), pce:
            with pytest.raises(RuntimeError, match="gate_blocked"):
                await _dispatch_worker(
                    issue_url="https://github.com/o/r/issues/42",
                    issue_num=42,
                    issue_title="Fix it",
                    slack_client=slack_client,
                    destination="C123",
                    payload={"worker_agent": "sam"},
                )

    async def test_non_json_handler_output_raises(self, slack_client):
        run = AsyncMock(return_value=("segfault: not json", "boom", 1))
        gam, _, pce = self._patches(None)
        with gam, patch("router.dispatcher._run_in_container", new=run), pce:
            with pytest.raises(RuntimeError, match="non-JSON"):
                await _dispatch_worker(
                    issue_url="https://github.com/o/r/issues/42",
                    issue_num=42,
                    issue_title="Fix it",
                    slack_client=slack_client,
                    destination="C123",
                    payload={"worker_agent": "sam"},
                )

    async def test_unknown_worker_agent_raises(self, slack_client):
        with patch("router.config.get_agent_map", return_value={"lisa": {"container": "agent-lisa"}}):
            with pytest.raises(RuntimeError, match="unknown worker agent"):
                await _dispatch_worker(
                    issue_url="https://github.com/o/r/issues/42",
                    issue_num=42,
                    issue_title="Fix it",
                    slack_client=slack_client,
                    destination="C123",
                    payload={"worker_agent": "ghost"},
                )

    async def test_thread_ts_forwarded_to_handler_command(self, slack_client):
        """#556: the kickoff thread_ts must appear in the --thread-ts arg so
        the dispatch handler receives a valid Slack context."""
        run = AsyncMock(return_value=('{"status": "launched", "dispatch_id": "d2", "pid": 10}', "", 0))
        extras = MagicMock()
        extras.env = {"WORKERS_BOT_TOKEN": "xoxb-test"}
        with (
            patch("router.config.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
            patch("router.dispatcher._run_in_container", new=run),
            patch("router.packs.dispatch_hook.pack_cli_extras", return_value=extras),
        ):
            await _dispatch_worker(
                issue_url="https://github.com/o/r/issues/42",
                issue_num=42,
                issue_title="Fix it",
                slack_client=slack_client,
                destination="C123",
                thread_ts="9999999999.000001",
                payload={"worker_agent": "sam"},
            )
        cmd = run.await_args.kwargs["command"]
        ts_idx = cmd.index("--thread-ts")
        assert cmd[ts_idx + 1] == "9999999999.000001"

    async def test_error_envelope_has_status(self, slack_client):
        """#556: when the dispatch handler rejects the call (e.g. missing
        thread_ts), the JSON envelope must include 'status' so _dispatch_worker
        raises a descriptive error rather than status=None."""
        run = AsyncMock(
            return_value=(
                '{"status": "error", "reason": "missing_slack_context", "detail": "missing --thread-ts"}',
                "",
                1,
            )
        )
        extras = MagicMock()
        extras.env = {}
        with (
            patch("router.config.get_agent_map", return_value={"sam": {"container": "agent-sam"}}),
            patch("router.dispatcher._run_in_container", new=run),
            patch("router.packs.dispatch_hook.pack_cli_extras", return_value=extras),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await _dispatch_worker(
                    issue_url="https://github.com/o/r/issues/42",
                    issue_num=42,
                    issue_title="Fix it",
                    slack_client=slack_client,
                    destination="C123",
                    thread_ts="",
                    payload={"worker_agent": "sam"},
                )
        # status must be present in the error message (not 'None')
        assert "status='error'" in str(exc_info.value)
        assert "missing_slack_context" in str(exc_info.value)


# ---------------------------------------------------------------------------
# register_auto_dispatch — scheduler hook (P1). Without this the tick is never
# imported by the scheduler and the whole loop is dead code.
# ---------------------------------------------------------------------------


@pytest.fixture
def task_store(tmp_path):
    from router.scheduled_tasks.store import ScheduledTaskStore

    s = ScheduledTaskStore(str(tmp_path / "tasks.db"))
    yield s


class TestRegisterAutoDispatch:
    def test_registers_new_task_with_tick_ref(self, task_store):
        from router import auto_dispatch as ad

        task = ad.register_auto_dispatch(task_store, agent_name="sam", repo="org/repo")
        assert task.callable_ref == ad.CALLABLE_REF == "router.auto_dispatch:tick"
        assert task.payload["repo"] == "org/repo"
        assert task.period_seconds == ad.DEFAULT_PERIOD_SECONDS

    def test_idempotent_on_second_call(self, task_store):
        from router import auto_dispatch as ad

        ad.register_auto_dispatch(task_store, agent_name="sam", repo="org/repo")
        ad.register_auto_dispatch(task_store, agent_name="sam", repo="org/repo")
        assert len(task_store.list_by_callable_ref(ad.CALLABLE_REF)) == 1

    def test_destination_threaded_into_payload(self, task_store):
        from router import auto_dispatch as ad

        task = ad.register_auto_dispatch(task_store, agent_name="sam", repo="org/repo", destination="C_BRAM")
        assert task.payload["destination"] == "C_BRAM"
