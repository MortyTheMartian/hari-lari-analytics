"""The result of a query: a DataFrame in one output convention, plus its metadata.

Columns, in order: the dimensions (a total row has them NULL, except the time bucket of a
bucket total), `is_total`, `is_other` (with `top: {other: true}`), `in_window` (with
`buckets_before`), one column per metric, and with a comparison `prior_<metric>`,
`<metric>_change` (ratio) and `<metric>_delta` (difference; for `pct` metrics in fractions).
Then `n_orders` and every count named in a metric's `min_n` as `n_<count>` (with `prior_n_*`),
and `trading_days` (with `prior_trading_days`) when the query has dimensions.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd

from hari_lari_analytics.reporting.registry import Dimension, Metric
from hari_lari_analytics.reporting.windows import Grain, ResolvedWindow

Shape = Literal["single", "rows", "series", "matrix", "decomposition"]


@dataclass(frozen=True)
class BucketInfo:
    """One time bucket of a series; `ordinal` is negative for the buckets before the window."""

    ordinal: int
    start: date
    end: date  # inclusive; the bucket may reach outside the window


@dataclass(frozen=True)
class ResultMeta:
    shape: Shape
    window: ResolvedWindow | None  # None for a @query source without a window
    empty: bool  # no sales in the window (after the filters)
    metrics: dict[str, Metric] = field(default_factory=dict)  # every metric and n_ column
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    trading_days: int | None = None
    prior_trading_days: int | None = None
    grain: Grain | None = None  # series only
    buckets: tuple[BucketInfo, ...] = ()  # series only, oldest first
    prior_buckets: tuple[BucketInfo, ...] = ()  # aligned by ordinal
    sql: str | None = None
    params: dict = field(default_factory=dict)

    @property
    def compare_ok(self) -> bool:
        return self.window is not None and self.window.compare_ok

    @property
    def has_comparison(self) -> bool:
        """The query asked for a comparison (its columns exist, NULL when not `compare_ok`)."""
        return self.window is not None and self.window.compare != "none"


@dataclass(frozen=True)
class Result:
    df: pd.DataFrame
    meta: ResultMeta

    @property
    def rows(self) -> pd.DataFrame:
        """The rows without the total rows."""
        if "is_total" not in self.df:
            return self.df
        return self.df[~self.df["is_total"]]

    @property
    def total(self) -> pd.Series | None:
        """The total row over the whole window, if the result has one."""
        if "is_total" not in self.df:
            return None
        totals = self.df[self.df["is_total"]]
        dims = [d for d in self.meta.dimensions if d in totals]
        if dims:  # a bucket total of a series keeps its bucket
            totals = totals[totals[dims].isna().all(axis=1)]
        return totals.iloc[0] if len(totals) else None

    def replace(self, df: pd.DataFrame | None = None, **meta) -> Result:
        """A new result with another DataFrame and/or changed metadata (for transforms)."""
        new_meta = dataclasses.replace(self.meta, **meta) if meta else self.meta
        return Result(self.df if df is None else df, new_meta)
