"""Report periods anchored on the last sale date, plus their prior periods.

The old report uses L14D, L6W and L3M; the reporting framework (`reporting/windows.py`) builds
every window on the functions here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd


@dataclass(frozen=True)
class Period:
    key: str
    title: str
    start: date
    end: date  # inclusive
    grain: str  # "sale_date", "sale_week" or "sale_month"
    prior_start: date
    prior_end: date

    def mask(self, ts: pd.Series) -> pd.Series:
        return _between(ts, self.start, self.end)

    def prior_mask(self, ts: pd.Series) -> pd.Series:
        return _between(ts, self.prior_start, self.prior_end)

    def label(self) -> str:
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"

    def prior_label(self) -> str:
        return f"{self.prior_start:%d %b %Y} – {self.prior_end:%d %b %Y}"


def _between(ts: pd.Series, start: date, end: date) -> pd.Series:
    return (ts >= pd.Timestamp(start)) & (ts < pd.Timestamp(end + timedelta(days=1)))


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
    """The last n complete ISO weeks (Monday–Sunday) up to the anchor date."""
    end = anchor if anchor.weekday() == 6 else anchor - timedelta(days=anchor.weekday() + 1)
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
    """The anchor's ISO week up to the anchor, against the same weekdays a week earlier."""
    start = anchor - timedelta(days=anchor.weekday())
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


def last_14_days(anchor: date) -> Period:
    return last_n_days(anchor, 14)


def last_6_weeks(anchor: date) -> Period:
    return last_n_weeks(anchor, 6)


def last_3_months(anchor: date) -> Period:
    return last_n_months(anchor, 3)


def report_periods(anchor: date) -> list[Period]:
    return [last_14_days(anchor), last_6_weeks(anchor), last_3_months(anchor)]
