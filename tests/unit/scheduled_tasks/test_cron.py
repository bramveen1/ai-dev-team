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

    def test_day_of_week_range_ending_in_seven(self):
        # 1-7 (Mon–Sun) must not raise; blind replace turned it into 1-0 (reversed range).
        cron.validate("0 0 * * 1-7")

    def test_day_of_week_range_zero_to_seven(self):
        # 0-7 spans Sun–Sun (all days); blind replace collapsed it to 0-0.
        cron.validate("0 0 * * 0-7")

    def test_day_of_week_step_seven(self):
        # */7 means every-7th starting from 0 within [0,6] = {0}; blind replace made step 0.
        cron.validate("0 0 * * */7")

    def test_day_of_week_list_with_seven(self):
        # 0,7 should be accepted and treated as two aliases for Sunday.
        cron.validate("0 0 * * 0,7")

    def test_day_of_week_range_six_to_seven_is_weekend(self):
        # 6-7 (Sat-Sun weekend) must not raise; blind replace turned it into 6-0 (reversed range).
        cron.validate("0 0 * * 6-7")

    def test_day_of_week_seven_maps_to_sunday_set(self):
        fields = cron.parse("0 0 * * 7")
        assert 0 in fields[4] and 7 not in fields[4]

    def test_day_of_week_zero_maps_to_sunday_set(self):
        fields = cron.parse("0 0 * * 0")
        assert fields[4] == {0}

    def test_day_of_week_range_1_to_7_is_all_days(self):
        fields = cron.parse("0 0 * * 1-7")
        assert fields[4] == {0, 1, 2, 3, 4, 5, 6}

    def test_day_of_week_range_6_to_7_is_weekend_set(self):
        fields = cron.parse("0 0 * * 6-7")
        assert fields[4] == {0, 6}

    def test_day_of_week_step_7_is_sunday_only(self):
        fields = cron.parse("0 0 * * */7")
        assert fields[4] == {0}

    def test_day_of_week_list_0_and_7_is_sunday_only(self):
        fields = cron.parse("0 0 * * 0,7")
        assert fields[4] == {0}

    def test_rejects_day_of_week_out_of_range_17(self):
        with pytest.raises(cron.CronError):
            cron.validate("0 0 * * 17")

    def test_rejects_day_of_week_out_of_range_8(self):
        with pytest.raises(cron.CronError):
            cron.validate("0 0 * * 8")

    def test_rejects_feb_30(self):
        with pytest.raises(cron.CronError, match="Impossible"):
            cron.validate("0 0 30 2 *")

    def test_rejects_apr_31(self):
        with pytest.raises(cron.CronError, match="Impossible"):
            cron.validate("0 0 31 4 *")

    def test_accepts_feb_29(self):
        # Feb 29 exists on leap years — not an impossible schedule.
        cron.validate("0 0 29 2 *")

    def test_rejects_day_31_in_all_30day_months(self):
        with pytest.raises(cron.CronError, match="Impossible"):
            cron.validate("0 0 31 4,6,9,11 *")

    def test_accepts_day_31_when_some_month_has_31_days(self):
        # Jan (month 1) has 31 days, so day 31 in months 1,2 is reachable.
        cron.validate("0 0 31 1,2 *")

    def test_restricted_dow_bypasses_dom_month_check(self):
        # DOW=Mon–Fri means the job can fire on any weekday regardless of DOM.
        cron.validate("0 0 30 2 1-5")


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

    def test_impossible_cron_raises_immediately(self):
        # Must raise CronError without iterating 527k steps.
        after = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(cron.CronError, match="Impossible"):
            cron.next_run_after("0 0 30 2 *", after)

    def test_feb29_returns_next_leap_year(self):
        # From 2026-06-28, the next Feb 29 is 2028-02-29 — 581 days away.
        # Without month-skipping this would exhaust the default 366-day window.
        after = datetime(2026, 6, 28, 0, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 0 29 2 *", after)
        assert result == datetime(2028, 2, 29, 0, 0, tzinfo=timezone.utc)

    def test_feb29_from_day_after_leap_day(self):
        # From 2028-03-01, the next Feb 29 is 2032-02-29 (4 years away).
        after = datetime(2028, 3, 1, 0, 0, tzinfo=timezone.utc)
        result = cron.next_run_after("0 0 29 2 *", after)
        assert result == datetime(2032, 2, 29, 0, 0, tzinfo=timezone.utc)
