"""Unit tests for packs/dispatch/quota.py (D-5 / #158).

Covers:
- window_state: rolling-window cost scan, skips infrastructure dirs.
- is_locked: reads sentinel, auto-clears after window_hours.
- mark_locked: atomic write.
- maybe_post_warning: posts exactly once per window, idempotent.
- log_window_oneliner: calls the injected log_fn with correct values.
- load_config: reads YAML, falls back to defaults on any error.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_quota():
    """Import quota.py without polluting sys.modules globally."""
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test_quota", PACK_DIR / "quota.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def quota():
    return _load_quota()


def _make_dispatch(root: Path, dispatch_id: str, *, started_at: datetime, cost: float | None = None) -> Path:
    """Write the minimal sidecar files for a dispatch."""
    d = root / dispatch_id
    d.mkdir()
    (d / "started_at").write_text(started_at.isoformat())
    if cost is not None:
        (d / "cost").write_text(str(cost))
    return d


def _utc(hours_ago: float = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


# ── window_state ────────────────────────────────────────────────────────────


class TestWindowState:
    def test_empty_root_returns_zeros(self, quota, tmp_path: Path) -> None:
        cost, count, oldest = quota.window_state(tmp_path, _utc())
        assert cost == 0.0
        assert count == 0
        assert oldest is None

    def test_missing_root_returns_zeros(self, quota, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-dir"
        cost, count, oldest = quota.window_state(missing, _utc())
        assert cost == 0.0 and count == 0 and oldest is None

    def test_sums_cost_files_within_window(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-001", started_at=_utc(1), cost=10.0)
        _make_dispatch(tmp_path, "d-002", started_at=_utc(2), cost=15.5)
        _make_dispatch(tmp_path, "d-003", started_at=_utc(3), cost=5.0)
        cost, count, oldest = quota.window_state(tmp_path, now, window_hours=5)
        assert abs(cost - 30.5) < 0.001
        assert count == 3

    def test_excludes_dispatches_outside_window(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-in", started_at=_utc(4), cost=20.0)
        _make_dispatch(tmp_path, "d-out", started_at=_utc(6), cost=999.0)  # older than 5h window
        cost, count, oldest = quota.window_state(tmp_path, now, window_hours=5)
        assert abs(cost - 20.0) < 0.001
        assert count == 1

    def test_skips_dot_dirs(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-real", started_at=_utc(1), cost=5.0)
        hidden = tmp_path / ".slots"
        hidden.mkdir()
        (hidden / "started_at").write_text(_utc(1).isoformat())
        (hidden / "cost").write_text("999.0")
        cost, count, _ = quota.window_state(tmp_path, now)
        assert abs(cost - 5.0) < 0.001
        assert count == 1

    def test_skips_underscore_dirs(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-real", started_at=_utc(1), cost=7.0)
        orphan = tmp_path / "_orphans"
        orphan.mkdir()
        (orphan / "started_at").write_text(_utc(1).isoformat())
        (orphan / "cost").write_text("888.0")
        cost, count, _ = quota.window_state(tmp_path, now)
        assert abs(cost - 7.0) < 0.001
        assert count == 1

    def test_dispatch_with_no_cost_file_contributes_zero(self, quota, tmp_path: Path) -> None:
        _make_dispatch(tmp_path, "d-inflight", started_at=_utc(1), cost=None)
        cost, count, _ = quota.window_state(tmp_path, _utc())
        assert cost == 0.0
        assert count == 1  # counted but zero cost

    def test_dispatch_with_invalid_cost_file_contributes_zero(self, quota, tmp_path: Path) -> None:
        d = _make_dispatch(tmp_path, "d-bad", started_at=_utc(1))
        (d / "cost").write_text("not-a-number")
        cost, count, _ = quota.window_state(tmp_path, _utc())
        assert cost == 0.0
        assert count == 1

    def test_returns_oldest_started_at(self, quota, tmp_path: Path) -> None:
        older = _utc(3)
        newer = _utc(1)
        _make_dispatch(tmp_path, "d-older", started_at=older, cost=1.0)
        _make_dispatch(tmp_path, "d-newer", started_at=newer, cost=1.0)
        _, _, oldest = quota.window_state(tmp_path, _utc())
        # oldest.replace(tzinfo=None) for comparison when both have tz
        assert oldest is not None
        assert abs((oldest - older).total_seconds()) < 2


# ── is_locked ───────────────────────────────────────────────────────────────


class TestIsLocked:
    def test_no_sentinel_returns_false(self, quota, tmp_path: Path) -> None:
        locked, retry = quota.is_locked(tmp_path, _utc())
        assert locked is False
        assert retry is None

    def test_active_lock_returns_true_with_retry(self, quota, tmp_path: Path) -> None:
        now = _utc()
        locked_at = now - timedelta(hours=1)
        (tmp_path / ".quota_locked").write_text(locked_at.isoformat())

        locked, retry = quota.is_locked(tmp_path, now, window_hours=5)
        assert locked is True
        assert retry is not None
        # retry_after should be approximately 4h from now
        retry_dt = datetime.fromisoformat(retry)
        assert abs((retry_dt - now).total_seconds() - 4 * 3600) < 5

    def test_auto_clears_after_window(self, quota, tmp_path: Path) -> None:
        now = _utc()
        locked_at = now - timedelta(hours=6)  # locked 6h ago, window=5h → expired
        (tmp_path / ".quota_locked").write_text(locked_at.isoformat())

        locked, retry = quota.is_locked(tmp_path, now, window_hours=5)
        assert locked is False
        assert retry is None

    def test_exactly_at_boundary_clears(self, quota, tmp_path: Path) -> None:
        now = _utc()
        locked_at = now - timedelta(hours=5)
        (tmp_path / ".quota_locked").write_text(locked_at.isoformat())

        locked, _ = quota.is_locked(tmp_path, now, window_hours=5)
        assert locked is False

    def test_invalid_sentinel_content_returns_false(self, quota, tmp_path: Path) -> None:
        (tmp_path / ".quota_locked").write_text("not-a-timestamp")
        locked, retry = quota.is_locked(tmp_path, _utc())
        assert locked is False
        assert retry is None


# ── mark_locked ─────────────────────────────────────────────────────────────


class TestMarkLocked:
    def test_writes_sentinel_with_iso_timestamp(self, quota, tmp_path: Path) -> None:
        now = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)
        quota.mark_locked(tmp_path, now)
        sentinel = tmp_path / ".quota_locked"
        assert sentinel.exists()
        assert sentinel.read_text().strip() == now.isoformat()

    def test_idempotent_overwrite(self, quota, tmp_path: Path) -> None:
        now1 = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 5, 19, 11, 0, 0, tzinfo=timezone.utc)
        quota.mark_locked(tmp_path, now1)
        quota.mark_locked(tmp_path, now2)
        sentinel = tmp_path / ".quota_locked"
        assert sentinel.read_text().strip() == now2.isoformat()

    def test_is_locked_sees_mark_locked(self, quota, tmp_path: Path) -> None:
        now = _utc()
        quota.mark_locked(tmp_path, now)
        locked, _ = quota.is_locked(tmp_path, now, window_hours=5)
        assert locked is True


# ── maybe_post_warning ───────────────────────────────────────────────────────


class TestMaybePostWarning:
    def _slack_fn(self, posted: list) -> Any:
        def fn(channel, thread_ts, text):
            posted.append({"channel": channel, "thread_ts": thread_ts, "text": text})

        return fn

    def test_no_warning_below_80_percent(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-cheap", started_at=_utc(1), cost=30.0)  # 60% of 50
        posted: list = []
        result = quota.maybe_post_warning(tmp_path, now, self._slack_fn(posted), "C1", "1.0", threshold_usd=50.0)
        assert result is False
        assert posted == []

    def test_posts_warning_at_80_percent(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-heavy", started_at=_utc(1), cost=40.0)  # 80% of 50
        posted: list = []
        result = quota.maybe_post_warning(tmp_path, now, self._slack_fn(posted), "C1", "1.0", threshold_usd=50.0)
        assert result is True
        assert len(posted) == 1
        assert "40.00" in posted[0]["text"]

    def test_idempotent_within_window(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-heavy", started_at=_utc(1), cost=45.0)
        posted: list = []
        fn = self._slack_fn(posted)
        quota.maybe_post_warning(tmp_path, now, fn, "C1", "1.0", threshold_usd=50.0)
        quota.maybe_post_warning(tmp_path, now, fn, "C1", "1.0", threshold_usd=50.0)
        quota.maybe_post_warning(tmp_path, now, fn, "C1", "1.0", threshold_usd=50.0)
        assert len(posted) == 1  # exactly one post per window

    def test_sentinel_file_created(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-heavy", started_at=_utc(1), cost=45.0)
        quota.maybe_post_warning(tmp_path, now, self._slack_fn([]), "C1", "1.0", threshold_usd=50.0)
        sentinels = list(tmp_path.glob(".warning_sent_*"))
        assert len(sentinels) == 1

    def test_slack_post_failure_clears_sentinel_for_retry(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-heavy", started_at=_utc(1), cost=45.0)

        def boom(ch, ts, txt):
            raise RuntimeError("Slack down")

        result = quota.maybe_post_warning(tmp_path, now, boom, "C1", "1.0", threshold_usd=50.0)
        assert result is False
        # Sentinel is removed on failure so a subsequent call can retry.
        assert len(list(tmp_path.glob(".warning_sent_*"))) == 0

    def test_slack_post_failure_allows_retry(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-heavy", started_at=_utc(1), cost=45.0)
        calls: list = []

        def flaky(ch, ts, txt):
            calls.append("attempt")
            if len(calls) == 1:
                raise RuntimeError("Slack down")

        result1 = quota.maybe_post_warning(tmp_path, now, flaky, "C1", "1.0", threshold_usd=50.0)
        assert result1 is False
        assert len(calls) == 1
        # Sentinel cleared after failure — next call retries and succeeds.
        result2 = quota.maybe_post_warning(tmp_path, now, flaky, "C1", "1.0", threshold_usd=50.0)
        assert result2 is True
        assert len(calls) == 2

    def test_concurrent_calls_post_exactly_once(self, quota, tmp_path: Path) -> None:
        """With O_CREAT|O_EXCL sentinel, concurrent callers post exactly once."""
        import threading

        now = _utc()
        _make_dispatch(tmp_path, "d-heavy", started_at=_utc(1), cost=45.0)
        posted: list = []
        fn = self._slack_fn(posted)
        errors: list = []

        def call_warning():
            try:
                quota.maybe_post_warning(tmp_path, now, fn, "C1", "1.0", threshold_usd=50.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call_warning) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(posted) == 1


# ── log_window_oneliner ──────────────────────────────────────────────────────


class TestLogWindowOneliner:
    def test_calls_log_fn_with_cost_count_elapsed(self, quota, tmp_path: Path) -> None:
        now = _utc()
        _make_dispatch(tmp_path, "d-001", started_at=_utc(2), cost=12.5)
        _make_dispatch(tmp_path, "d-002", started_at=_utc(1), cost=7.5)
        logged: list[tuple] = []
        quota.log_window_oneliner(tmp_path, now, lambda fmt, *args: logged.append((fmt, args)))
        assert len(logged) == 1
        fmt, args = logged[0]
        assert "$" in fmt or "%.2f" in fmt
        assert abs(args[0] - 20.0) < 0.01  # total cost
        assert args[1] == 2  # dispatch count

    def test_empty_root_logs_zeros(self, quota, tmp_path: Path) -> None:
        logged: list = []
        quota.log_window_oneliner(tmp_path, _utc(), lambda fmt, *args: logged.append(args))
        assert len(logged) == 1
        assert logged[0][0] == 0.0
        assert logged[0][1] == 0

    def test_uses_logger_info_when_no_log_fn(self, quota, tmp_path: Path) -> None:
        # Should not raise even with the default logger.
        quota.log_window_oneliner(tmp_path, _utc())


# ── load_config ──────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_reads_threshold_and_window(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
            quota:
              threshold_usd: 75
              window_hours: 8
        """)
        )
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == 75.0
        assert cfg["window_hours"] == 8.0

    def test_missing_file_returns_defaults(self, quota, tmp_path: Path) -> None:
        cfg = quota.load_config(tmp_path / "nonexistent.yaml")
        assert cfg["threshold_usd"] == quota.DEFAULT_THRESHOLD_USD
        assert cfg["window_hours"] == quota.DEFAULT_WINDOW_HOURS

    def test_missing_quota_section_returns_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("other_section:\n  key: value\n")
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == quota.DEFAULT_THRESHOLD_USD
        assert cfg["window_hours"] == quota.DEFAULT_WINDOW_HOURS

    def test_partial_quota_section_fills_remaining_with_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("quota:\n  threshold_usd: 100\n")
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == 100.0
        assert cfg["window_hours"] == quota.DEFAULT_WINDOW_HOURS

    def test_empty_yaml_returns_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("")
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == quota.DEFAULT_THRESHOLD_USD

    def test_malformed_yaml_returns_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("quota:\n  threshold_usd: [\n")  # unclosed bracket
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == quota.DEFAULT_THRESHOLD_USD
        assert cfg["window_hours"] == quota.DEFAULT_WINDOW_HOURS

    def test_non_numeric_threshold_usd_returns_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("quota:\n  threshold_usd: not-a-number\n")
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == quota.DEFAULT_THRESHOLD_USD
        assert cfg["window_hours"] == quota.DEFAULT_WINDOW_HOURS

    def test_non_numeric_window_hours_returns_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("quota:\n  window_hours: not-a-number\n")
        cfg = quota.load_config(cfg_file)
        assert cfg["threshold_usd"] == quota.DEFAULT_THRESHOLD_USD
        assert cfg["window_hours"] == quota.DEFAULT_WINDOW_HOURS

    def test_real_dispatch_yaml_in_repo(self, quota) -> None:
        """The shipped config/dispatch.yaml must parse to the documented defaults."""
        config_yaml = REPO_ROOT / "config" / "dispatch.yaml"
        if not config_yaml.exists():
            pytest.skip("config/dispatch.yaml not present")
        cfg = quota.load_config(config_yaml)
        assert cfg["threshold_usd"] == 50.0
        assert cfg["window_hours"] == 5.0


# ── window_start_unix ─────────────────────────────────────────────────────────


class TestWindowStartUnix:
    def test_aligns_to_window_boundary(self, quota) -> None:
        # 5-hour window (18000s). If now = epoch + 7h, window_start = epoch + 5h.
        epoch_plus_7h = datetime(1970, 1, 1, 7, 0, 0, tzinfo=timezone.utc)
        result = quota.window_start_unix(epoch_plus_7h, 5.0)
        assert result == 5 * 3600  # epoch + 5h

    def test_stable_within_same_window(self, quota) -> None:
        t1 = datetime(2026, 5, 19, 8, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 19, 9, 30, 0, tzinfo=timezone.utc)
        assert quota.window_start_unix(t1, 5.0) == quota.window_start_unix(t2, 5.0)

    def test_different_across_window_boundary(self, quota) -> None:
        # 5h window: crossing the boundary produces a different start.
        t1 = datetime(1970, 1, 1, 4, 59, 59, tzinfo=timezone.utc)
        t2 = datetime(1970, 1, 1, 5, 0, 1, tzinfo=timezone.utc)
        assert quota.window_start_unix(t1, 5.0) != quota.window_start_unix(t2, 5.0)
