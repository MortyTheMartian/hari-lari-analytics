"""Windows: date ranges relative to the anchor, their comparison, time buckets and trading days.

A window name is one of `L<n>D`, `L<n>W`, `L<n>M` (so `L14D`, `L6W`, `L3M`), the same as
`last_n_days(n)`, `last_n_weeks(n)` and `last_n_months(n)`, or `WTD`, `MTD` and `full`, each a
`Period` anchored on the last sale date with its own prior period. The comparison is
`prior` (the period's own prior: the same length directly before, or the same weekdays a week
earlier for windows shorter than a week, WTD and MTD), `last_year` (364 days earlier, so
weekdays line up; 12 months earlier for month windows, MTD and `full`) or `none`. A comparison
that starts before the first sale is not valid (`compare_ok` is false): the report shows `n/a`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import duckdb

from hari_lari_analytics.core.project import project

Grain = Literal["day", "week", "month", "block_7d"]
Compare = Literal["prior", "last_year", "none"]

GRAINS: tuple[Grain, ...] = ("day", "week", "month", "block_7d")
GRAIN_COLUMN = {"day": "sale_date", "week": "sale_week", "month": "sale_month"}
_PERIOD_GRAIN: dict[str, Grain] = {"sale_date": "day", "sale_week": "week", "sale_month": "month"}

_SHORT = re.compile(r"^L(\d+)([DWM])$")
_LONG = re.compile(r"^last_n_(days|weeks|months)\((\d+)\)$")
_UNIT = {"D": "days", "W": "weeks", "M": "months"}


@dataclass(frozen=True)
class DayEvent:
    """The calendar event of one day (config/calendar_events.csv through `dim_calendar`)."""

    date: date
    kind: str  # the most telling kind of the day: closure, holiday, promotion or other
    name: str  # several names joined by ' + '


@dataclass(frozen=True)
class Calendar:
    """What the resolver needs from a run: the anchor, the first sale and the trading days, plus
    the calendar events, which the compiler hands to the results of time queries."""

    anchor: date
    first_sale: date | None  # None: no sales at all
    trading_dates: frozenset[date] = frozenset()
    events: tuple[DayEvent, ...] = ()  # by date

    @classmethod
    def from_db(cls, con: duckdb.DuckDBPyConnection) -> Calendar:
        anchor = con.execute("select anchor from run_context").fetchone()[0]
        days = con.execute(
            "select date, is_trading_day, event_kind, event_name from dim_calendar order by date"
        ).fetchall()
        return cls(
            anchor=anchor,
            first_sale=min((d for d, *_ in days), default=None),
            trading_dates=frozenset(d for d, trading, *_ in days if trading),
            events=tuple(DayEvent(d, kind, name) for d, _, kind, name in days if kind),
        )

    def trading_days(self, start: date, end: date) -> int:
        return sum(start <= d <= end for d in self.trading_dates)

    def events_between(self, start: date, end: date) -> tuple[DayEvent, ...]:
        return tuple(e for e in self.events if start <= e.date <= end)

    def in_history(self, start: date) -> bool:
        """True when a range starting on `start` lies fully within the sales history."""
        return self.first_sale is not None and start >= self.first_sale


@dataclass(frozen=True)
class Window:
    start: date
    end: date  # inclusive
    grain: Grain  # the default time grain of a trend over this window
    title: str
    trading_days: int

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def end_exclusive(self) -> date:
        return self.end + timedelta(days=1)

    @property
    def label(self) -> str:
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True)
class ResolvedWindow:
    """A window with its comparison; `comparison` is None for `compare: none`."""

    key: str  # normalised name: last_n_weeks(6) becomes L6W
    current: Window
    compare: Compare
    comparison: Window | None
    compare_ok: bool  # a comparison exists and starts on or after the first sale
    compare_label: str | None  # "prior 6 weeks", "same weekday last week", ...


@dataclass(frozen=True)
class Bucket:
    """One time bucket; it may reach outside the window (a week that starts before it)."""

    start: date
    end: date  # inclusive


def parse_window(name: str) -> tuple[str, int | None]:
    """`("days", 14)` for L14D or last_n_days(14); `("WTD", None)` for WTD, MTD and full."""
    if name in ("WTD", "MTD", "full"):
        return name, None
    if m := _SHORT.match(name):
        unit, n = _UNIT[m[2]], int(m[1])
    elif m := _LONG.match(name):
        unit, n = m[1], int(m[2])
    else:
        raise ValueError(
            f"unknown window {name!r}: use L<n>D, L<n>W, L<n>M, last_n_days(n), "
            "last_n_weeks(n), last_n_months(n), WTD, MTD or full"
        )
    if n < 1:
        raise ValueError(f"window {name!r}: n must be at least 1")
    return unit, n


# ---- periods: the date ranges of the window names, each with its prior period ----------


@dataclass(frozen=True)
class Period:
    key: str
    title: str
    start: date
    end: date  # inclusive
    grain: str  # "sale_date", "sale_week" or "sale_month"
    prior_start: date
    prior_end: date


def _month_start(d: date, months_back: int) -> date:
    index = d.year * 12 + d.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def _month_end(d: date) -> date:
    return _month_start(d, -1) - timedelta(days=1)


def shift_months(d: date, months: int) -> date:
    """The same day `months` months later (negative: earlier), cut to the month's last day.

    A month's last day maps to the last day of the target month (28 Feb 2029 -> 29 Feb 2028).
    """
    start = _month_start(d, -months)
    last = _month_end(start).day
    return start.replace(day=last if d == _month_end(d) else min(d.day, last))


def _plural(n: int, unit: str) -> str:
    return unit if n == 1 else f"{n} {unit}s"


def last_n_days(anchor: date, n: int) -> Period:
    """The n days up to the anchor, against the n days before.

    A window shorter than a week is compared with the same weekdays a week earlier instead, so
    a Saturday is compared with the Saturday before.
    """
    start = anchor - timedelta(days=n - 1)
    back = timedelta(days=7 if n < 7 else n)
    return Period(
        key=f"L{n}D",
        title=f"Last {_plural(n, 'day')}",
        start=start,
        end=anchor,
        grain="sale_date",
        prior_start=start - back,
        prior_end=anchor - back,
    )


def last_n_weeks(anchor: date, n: int) -> Period:
    """The last n complete weeks up to the anchor date (the week starts on `first_weekday` of
    project.yml: Monday–Sunday by default)."""
    week = project()
    end = anchor if anchor == week.week_end(anchor) else week.week_start(anchor) - timedelta(days=1)
    start = end - timedelta(days=7 * n - 1)
    return Period(
        key=f"L{n}W",
        title=f"Last {_plural(n, 'complete week')}",
        start=start,
        end=end,
        grain="sale_week",
        prior_start=start - timedelta(days=7 * n),
        prior_end=start - timedelta(days=1),
    )


def last_n_months(anchor: date, n: int) -> Period:
    """The last n complete calendar months up to the anchor date."""
    last_month = anchor if _month_end(anchor) == anchor else _month_start(anchor, 1)
    start = _month_start(last_month, n - 1)
    return Period(
        key=f"L{n}M",
        title=f"Last {_plural(n, 'complete month')}",
        start=start,
        end=_month_end(last_month),
        grain="sale_month",
        prior_start=_month_start(start, n),
        prior_end=start - timedelta(days=1),
    )


def week_to_date(anchor: date) -> Period:
    """The anchor's week up to the anchor, against the same weekdays a week earlier."""
    start = project().week_start(anchor)
    return Period(
        key="WTD",
        title="Week to date",
        start=start,
        end=anchor,
        grain="sale_date",
        prior_start=start - timedelta(days=7),
        prior_end=anchor - timedelta(days=7),
    )


def month_to_date(anchor: date) -> Period:
    """The anchor's month up to the anchor, against the same days of the month before.

    The comparison stops at the end of the month before when that month is shorter.
    """
    start = anchor.replace(day=1)
    prior_start = _month_start(anchor, 1)
    return Period(
        key="MTD",
        title="Month to date",
        start=start,
        end=anchor,
        grain="sale_date",
        prior_start=prior_start,
        prior_end=prior_start.replace(day=min(anchor.day, _month_end(prior_start).day)),
    )


def full_history(anchor: date, first_sale: date) -> Period:
    """From the first sale to the anchor; its prior period always lies before the first sale."""
    days = timedelta(days=(anchor - first_sale).days + 1)
    return Period(
        key="full",
        title="Full history",
        start=first_sale,
        end=anchor,
        grain="sale_month",
        prior_start=first_sale - days,
        prior_end=first_sale - timedelta(days=1),
    )


def _period(name: str, calendar: Calendar) -> Period:
    kind, n = parse_window(name)
    anchor = calendar.anchor
    match kind:
        case "days":
            return last_n_days(anchor, n)
        case "weeks":
            return last_n_weeks(anchor, n)
        case "months":
            return last_n_months(anchor, n)
        case "WTD":
            return week_to_date(anchor)
        case "MTD":
            return month_to_date(anchor)
        case _:
            return full_history(anchor, calendar.first_sale or anchor)


def _prior_label(kind: str, n: int | None) -> str:
    match kind:
        case "days" if n == 1:
            return "same weekday last week"
        case "days" if n < 7:
            return "same days last week"
        case "WTD":
            return "same days last week"
        case "MTD":
            return "same days last month"
        case "full":
            return "prior period"
    unit = kind.removesuffix("s")
    return f"prior {unit}" if n == 1 else f"prior {n} {kind}"


def _window(start: date, end: date, grain: Grain, title: str, calendar: Calendar) -> Window:
    return Window(start, end, grain, title, calendar.trading_days(start, end))


def resolve(name: str, calendar: Calendar, compare: Compare = "prior") -> ResolvedWindow:
    """Turn a window name into the current window and its comparison."""
    kind, n = parse_window(name)
    period = _period(name, calendar)
    grain = _PERIOD_GRAIN[period.grain]
    current = _window(period.start, period.end, grain, period.title, calendar)
    match compare:
        case "none":
            return ResolvedWindow(period.key, current, compare, None, False, None)
        case "prior":
            start, end, label = period.prior_start, period.prior_end, _prior_label(kind, n)
        case "last_year":
            if kind in ("months", "MTD", "full"):
                start, end = shift_months(period.start, -12), shift_months(period.end, -12)
            else:
                start, end = period.start - timedelta(364), period.end - timedelta(364)
            label = "same period last year"
        case _:
            raise ValueError(f"unknown comparison {compare!r}: use prior, last_year or none")
    comparison = _window(start, end, grain, label[0].upper() + label[1:], calendar)
    return ResolvedWindow(
        period.key, current, compare, comparison, calendar.in_history(start), label
    )


def bucket_start(d: date, grain: Grain, window_end: date | None = None) -> date:
    """The first day of the bucket holding `d`; 7-day blocks count back from `window_end`."""
    match grain:
        case "day":
            return d
        case "week":
            return project().week_start(d)
        case "month":
            return d.replace(day=1)
        case "block_7d":
            if window_end is None:
                raise ValueError("block_7d needs the window end")
            return window_end - timedelta(days=6 + (window_end - d).days // 7 * 7)
    raise ValueError(f"unknown grain {grain!r}: use {', '.join(GRAINS)}")


def _bucket(start: date, grain: Grain) -> Bucket:
    match grain:
        case "day":
            return Bucket(start, start)
        case "week" | "block_7d":
            return Bucket(start, start + timedelta(days=6))
        case _:
            return Bucket(start, shift_months(start, 1) - timedelta(days=1))


def buckets(window: Window, grain: Grain | None = None) -> list[Bucket]:
    """The time buckets of a window, oldest first.

    Weeks and months are calendar buckets, so the first and last may reach outside the window.
    7-day blocks are counted back from the window end, so the latest block is a full week;
    leftover days at the start of the window get no block (as the sparklines of today's report).
    """
    grain = grain or window.grain
    if grain == "block_7d":
        n = window.days // 7
        return [_bucket(window.end - timedelta(days=7 * k + 6), grain) for k in reversed(range(n))]
    result = [_bucket(bucket_start(window.start, grain), grain)]
    while result[-1].end < window.end:
        result.append(_bucket(result[-1].end + timedelta(days=1), grain))
    return result


def bucket_before(window: Window, grain: Grain | None = None) -> Bucket:
    """The bucket just before the window's first bucket, for growth labels on the first bucket.

    Use it only when it lies fully within the sales history (`Calendar.in_history`).
    """
    grain = grain or window.grain
    first = buckets(window, grain)
    if grain == "block_7d":
        start = (first[0].start if first else window.end - timedelta(days=6)) - timedelta(days=7)
        return _bucket(start, grain)
    return _bucket(bucket_start(first[0].start - timedelta(days=1), grain), grain)
