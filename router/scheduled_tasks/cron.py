"""Minimal 5-field cron expression parser.

Supports the standard POSIX cron syntax:
    minute  hour  day-of-month  month  day-of-week

Each field may be:
    *           any value
    N           literal number
    N-M         inclusive range
    A,B,C       comma-separated list of values or ranges
    */N         step (every N starting from the field's minimum)
    A-B/N       step within a range

Day-of-week: 0 = Sunday, 6 = Saturday. Day-of-week 7 is also Sunday.

This is a deliberately small subset (no @hourly/@daily aliases, no seconds,
no month/weekday names) — enough to cover the scheduling needs of the agent
system without pulling in an external dependency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

FIELD_RANGES = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week (0 = Sunday)
]

# Maximum days in each month; February uses 29 so leap-year schedules are accepted.
_MONTH_MAX_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Expand a single cron field into the set of matching integers."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Empty element in cron field: {field!r}")

        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str.isdigit() or int(step_str) < 1:
                raise CronError(f"Invalid step {step_str!r} in field {field!r}")
            step = int(step_str)
        else:
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            lo_str, hi_str = base.split("-", 1)
            start, end = _to_int(lo_str, field), _to_int(hi_str, field)
            if start > end:
                raise CronError(f"Reversed range {base!r} in field {field!r}")
        else:
            start = end = _to_int(base, field)

        if start < min_val or end > max_val:
            raise CronError(f"Value {base!r} out of range [{min_val},{max_val}] in field {field!r}")

        values.update(range(start, end + 1, step))
    return values


def _to_int(value: str, field: str) -> int:
    if not value.lstrip("-").isdigit():
        raise CronError(f"Non-numeric value {value!r} in field {field!r}")
    return int(value)


def parse(expression: str) -> list[set[int]]:
    """Parse a cron expression into five sets of matching integers.

    Returns:
        A list [minutes, hours, days_of_month, months, days_of_week] where
        each element is the set of matching integers for that field.
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise CronError(f"Cron expression must have 5 fields, got {len(parts)}: {expression!r}")

    fields = []
    for i, (part, (lo, hi)) in enumerate(zip(parts, FIELD_RANGES)):
        if i == 4:
            # Parse DOW allowing 7 (Sunday alias) as a valid bound/literal, then
            # collapse 7 → 0 so the result set stays within [0, 6].
            raw = _parse_field(part, lo, 7)
            fields.append({v % 7 for v in raw})
        else:
            fields.append(_parse_field(part, lo, hi))

    return fields


def _matches(moment: datetime, fields: list[set[int]]) -> bool:
    """Return True if ``moment`` matches the parsed cron fields.

    Per POSIX cron semantics, when both day-of-month and day-of-week are
    restricted (neither is a bare ``*``), a match in either is sufficient.
    """
    minute, hour, dom, month, dow = fields
    py_dow = (moment.weekday() + 1) % 7  # Monday=0..Sunday=6 -> Sunday=0..Saturday=6

    if moment.minute not in minute or moment.hour not in hour or moment.month not in month:
        return False

    dom_any = dom == set(range(FIELD_RANGES[2][0], FIELD_RANGES[2][1] + 1))
    dow_any = dow == set(range(FIELD_RANGES[4][0], FIELD_RANGES[4][1] + 1))

    if dom_any and dow_any:
        return moment.day in dom
    if dom_any:
        return py_dow in dow
    if dow_any:
        return moment.day in dom
    return moment.day in dom or py_dow in dow


def _check_dom_month_reachable(fields: list[set[int]]) -> None:
    """Raise CronError if the DOM values can never occur in any of the specified months.

    Skipped when DOW is restricted: per POSIX OR semantics a matching weekday fires
    the job regardless of DOM, so the expression remains satisfiable.
    """
    _minute, _hour, dom, months, dow = fields
    dom_any = dom == set(range(FIELD_RANGES[2][0], FIELD_RANGES[2][1] + 1))
    dow_any = dow == set(range(FIELD_RANGES[4][0], FIELD_RANGES[4][1] + 1))
    if dom_any or not dow_any:
        return
    if not any(_MONTH_MAX_DAYS[m] >= d for m in months for d in dom):
        raise CronError(f"Impossible schedule: day(s) {sorted(dom)!r} never occur in month(s) {sorted(months)!r}")


def next_run_after(expression: str, after: datetime, max_iterations: int = 366 * 24 * 60) -> datetime:
    """Compute the next datetime strictly after ``after`` that matches the cron expression.

    The returned datetime carries the same tzinfo as ``after`` (UTC is recommended).
    The search walks forward minute-by-minute within valid months; when the current
    month is not in the expression's month set the search jumps to the first minute
    of the next matching month rather than iterating through every minute.  This
    keeps the search efficient for infrequent schedules such as leap-day-only
    ``0 0 29 2 *``, which would otherwise exhaust the iteration cap before reaching
    the next February.
    """
    fields = parse(expression)
    _check_dom_month_reachable(fields)
    _minute_f, _hour_f, _dom_f, months, _dow_f = fields
    _sorted_months = sorted(months)

    moment = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(max_iterations):
        if moment.month not in months:
            next_m = next((m for m in _sorted_months if m > moment.month), None)
            if next_m is None:
                moment = moment.replace(year=moment.year + 1, month=_sorted_months[0], day=1, hour=0, minute=0)
            else:
                moment = moment.replace(month=next_m, day=1, hour=0, minute=0)
            continue
        if _matches(moment, fields):
            return moment
        moment += timedelta(minutes=1)

    raise CronError(f"No match found for {expression!r} within {max_iterations} minutes after {after.isoformat()}")


def validate(expression: str) -> None:
    """Raise CronError if the expression is not a valid 5-field cron string."""
    _check_dom_month_reachable(parse(expression))


def compute_next_run(expression: str, now: datetime | None = None) -> datetime:
    """Compute the next run datetime for ``expression`` using UTC ``now`` as the reference."""
    reference = now if now is not None else datetime.now(timezone.utc)
    return next_run_after(expression, reference)
