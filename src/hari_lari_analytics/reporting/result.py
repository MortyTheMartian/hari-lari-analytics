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
class MatrixSpec:
    """A pivoted result (shape `matrix`): `df` has the row dimension as its first column and one
    column per value of the column dimension, in `column_values` order."""

    rows: str  # dimension id of the rows
    columns: str  # dimension id of the columns
    value: str  # the df column shown in the cells, such as `orders` or `net_sales_change`
    column_values: tuple  # the values of the column dimension, as the df column labels


@dataclass(frozen=True)
class Classification:
    """What `classify` wrote into one column: the classes in their order, and the split values
    it used (the medians of a quadrant classification, the bounds of the bands)."""

    column: str
    mode: str
    classes: tuple[str, ...]
    splits: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DecompositionSpec:
    """What `decompose` split (shape `decomposition`): the KPI, the identity, and for a
    `sum_over` its dimension, whose values are in the df column of that name."""

    identity: str
    kind: str  # product, sum or sum_over
    kpi: str
    depth: int  # 2: each member of a sum_over is split with the identity's `then`
    dimension: str | None = None
    then: str | None = None


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
    matrix: MatrixSpec | None = None  # set by `pivot`
    classifications: dict[str, Classification] = field(default_factory=dict)  # by column
    decomposition: DecompositionSpec | None = None  # set by `decompose`

    @property
    def n_columns(self) -> dict[str, Metric]:
        """The sample-size columns (`n_orders`, ...) with the count metric each one holds."""
        return {c: m for c, m in self.metrics.items() if m.id != c}

    @property
    def value_metrics(self) -> dict[str, Metric]:
        """The metrics of the query, without the sample-size columns."""
        return {c: m for c, m in self.metrics.items() if m.id == c}

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

    def matrix_frame(self) -> pd.DataFrame:
        """A pivoted result as a plain matrix: the row values as the index, the column values as
        the columns."""
        if self.meta.matrix is None:
            raise ValueError("the result is not a matrix (use the pivot transform)")
        return self.df.set_index(self.meta.matrix.rows)

    def replace(self, df: pd.DataFrame | None = None, **meta) -> Result:
        """A new result with another DataFrame and/or changed metadata (for transforms)."""
        new_meta = dataclasses.replace(self.meta, **meta) if meta else self.meta
        return Result(self.df if df is None else df, new_meta)
