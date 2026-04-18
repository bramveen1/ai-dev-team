"""Tests for the minimal cron parser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from router.scheduled_tasks import cron


@pytest.mark.unit
class TestValidate:
    def test_five_fields_required(self):
        with pytest.raises(cron.CronError):
            cron.validate("* * * *")

    def test_accepts_star(self):
        cron.validate("* * * * *")

    def test_accepts_step(self):
        cron.validate("*/5 * * * *")

    def test_accepts_range(self):
        cron.validate("0 9-17 * * 1-5")

    def test_accepts_list(self):
        cron.validate("0 0 1,15 * *")

    def test_rejects_out_of_range_minute(self):
        with pytest.raises(cron.CronError):
            cron.validate("60 * * * *")

    def test_rejects_out_of_range_hour(self):
        with pytest.raises(cron.CronError):
            cron.validate("0 24 * * *")

    def test_rejects_non_numeric(self):
        with pytest.raises(cron.CronError):
            cron.validate("0 abc * * *")

    def test_day_of_week_seven_accepted_as_sunday(self):
        # 7 is an alias for 0 (Sunday) in some cron dialects.
        cron.validate("0 0 * * 7")


@pytest.mark.unit
class TestNextRunAfter:
    def test_every_minute(self):
        after = datetime(2026, 4, 17, 12, 30, 15, tzinfo=timezone.utc)
        result = cron.next_run_after("* * * * *", after)
        assert result == datetime(2026, 4, 17, 12, 31, tzinfo=timezone.utc)

    def test_strictly_after_when_currently_matching(self):
        # "0 9 * * *" and the reference is exactly 09:00 → should skip to tomorrow.
        after = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 9 * * *", after)
        assert result == datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)

    def test_daily_9am_weekdays_skips_weekend(self):
        # 2026-04-17 is a Friday; next weekday 9am is Monday 2026-04-20.
        after = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 9 * * 1-5", after)
        assert result == datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)

    def test_sunday_matches_weekday_zero(self):
        # Sunday 2026-04-19 at 09:00 UTC
        after = datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 9 * * 0", after)
        assert result == datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)

    def test_every_five_minutes(self):
        after = datetime(2026, 4, 17, 12, 32, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("*/5 * * * *", after)
        assert result == datetime(2026, 4, 17, 12, 35, tzinfo=timezone.utc)

    def test_first_of_month_at_midnight(self):
        after = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 0 1 * *", after)
        assert result == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

    def test_dom_or_dow_semantics(self):
        # "0 0 1 * 0" should fire on either day-of-month=1 OR Sunday — POSIX OR semantics.
        # After Sat 2026-04-18, the next match is Sun 2026-04-19 (not 2026-05-01).
        after = datetime(2026, 4, 18, 1, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 0 1 * 0", after)
        assert result == datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
