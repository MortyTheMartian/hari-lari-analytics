"""Report periods (L14D, L6W, L3M) anchored on the last sale date, plus their prior periods."""

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


def last_14_days(anchor: date) -> Period:
    start = anchor - timedelta(days=13)
    return Period(
        key="L14D",
        title="Last 14 days",
        start=start,
        end=anchor,
        grain="sale_date",
        prior_start=start - timedelta(days=14),
        prior_end=start - timedelta(days=1),
    )


def last_6_weeks(anchor: date) -> Period:
    """The last 6 complete ISO weeks (Monday–Sunday) up to the anchor date."""
    end = anchor if anchor.weekday() == 6 else anchor - timedelta(days=anchor.weekday() + 1)
    start = end - timedelta(days=41)
    return Period(
        key="L6W",
        title="Last 6 complete weeks",
        start=start,
        end=end,
        grain="sale_week",
        prior_start=start - timedelta(days=42),
        prior_end=start - timedelta(days=1),
    )


def last_3_months(anchor: date) -> Period:
    """The last 3 complete calendar months up to the anchor date."""
    last_month = anchor if _month_end(anchor) == anchor else _month_start(anchor, 1)
    start = _month_start(last_month, 2)
    return Period(
        key="L3M",
        title="Last 3 complete months",
        start=start,
        end=_month_end(last_month),
        grain="sale_month",
        prior_start=_month_start(start, 3),
        prior_end=start - timedelta(days=1),
    )


def report_periods(anchor: date) -> list[Period]:
    return [last_14_days(anchor), last_6_weeks(anchor), last_3_months(anchor)]
