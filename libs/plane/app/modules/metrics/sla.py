"""An endpoint's service level over a period: what was measured, what was promised, and the verdict.

Pure. The period is resolved and the verdict reached here, out of numbers the
backend already answered and a commitment the database already holds, so both
halves can be tested without a store or a session. `MetricsService.endpoint_sla`
is the part that fetches.

**The period.** A calendar month in UTC by default, because that is the unit an
SLA credit is written in; `from`/`to` for anything else. A period still running is
measured up to now and says so with `partial`, rather than being refused or being
extrapolated to its end. A period is measured as one lookback of its own length
taken at its end, so the figures are over exactly that span.

**The verdict.** `success_rate_objective` is a fraction in (0, 1] and the failure
ratio is a percentage on 0–100, so the allowance is `(1 - objective) * 100`. That
conversion is written once, in `verdict`, because a fraction compared with a
percentage is off by a hundred with no type error. Every verdict is `None` when
the reading it depends on is `None`: a `false` that means "we could not measure"
is worse than no answer.
"""

import calendar
import datetime as dt

from dataclasses import dataclass

from app.core.exceptions import Invalid


MAX_PERIOD = dt.timedelta(days=93)
"""The longest period one report covers. A quarter, and one query per figure over it."""

MIN_PERIOD = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class PeriodQuery:
    """What a caller asked for: a `YYYY-MM` month, a `from`/`to` range, or neither."""

    month: str | None = None
    start: dt.datetime | None = None
    end: dt.datetime | None = None


@dataclass(frozen=True)
class Period:
    start: dt.datetime
    end: dt.datetime
    partial: bool

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def lookback(self) -> str:
        return f"{self.seconds}s"


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def month_bounds(month: str) -> tuple[dt.datetime, dt.datetime]:
    """The first instant of a `YYYY-MM` month and of the month after it, in UTC."""
    try:
        year, number = (int(part) for part in month.split("-"))
        start = dt.datetime(year, number, 1, tzinfo=dt.UTC)
    except ValueError as exc:
        raise Invalid("month is YYYY-MM", {"month": month}) from exc
    days = calendar.monthrange(year, number)[1]
    return start, start + dt.timedelta(days=days)


def resolve_period(query: PeriodQuery, now: dt.datetime) -> Period:
    """The period a report is about, clamped to now.

    A month and an explicit range are two ways of saying the same thing and are
    refused together rather than one silently winning. With neither, the current
    calendar month.
    """
    now = _utc(now)
    month, start, end = query.month, query.start, query.end
    if month is not None and (start is not None or end is not None):
        raise Invalid("give either a month or from/to, not both")
    if month is not None:
        start, end = month_bounds(month)
    elif start is None and end is None:
        start, end = month_bounds(f"{now.year:04d}-{now.month:02d}")
    elif start is None:
        raise Invalid("to needs a from")
    else:
        start = _utc(start)
        end = _utc(end) if end is not None else now

    if start >= now:
        raise Invalid("the period has not started yet", {"from": start.isoformat()})
    if end <= start:
        raise Invalid("the period ends before it starts", {"from": start.isoformat(), "to": end.isoformat()})
    if end - start > MAX_PERIOD:
        raise Invalid("a report covers at most 93 days", {"days": (end - start).days})

    partial = end > now
    end = min(end, now)
    if end - start < MIN_PERIOD:
        raise Invalid("a report covers at least one minute")
    return Period(start=start, end=end, partial=partial)


def verdict(failure_ratio: float | None, success_rate: float | None) -> tuple[bool | None, float | None]:
    """Whether the objective was met, and how much of the error budget was spent.

    `failure_ratio` is a percentage and `success_rate` a fraction. The budget is the
    failure the objective allows; consumed is spent over allowed, so `1.0` is the
    whole budget and anything above it is a breach. An objective of exactly 1 has
    no budget to divide by, so consumed is `None` there while met is still answered.
    """
    if failure_ratio is None or success_rate is None:
        return None, None
    allowed = (1 - float(success_rate)) * 100
    met = failure_ratio <= allowed
    consumed = failure_ratio / allowed if allowed > 0 else None
    return met, consumed


def request_count(requests_per_second: float | None, period: Period) -> int | None:
    """Requests over the period, from the average rate across it."""
    if requests_per_second is None:
        return None
    return round(requests_per_second * period.seconds)
