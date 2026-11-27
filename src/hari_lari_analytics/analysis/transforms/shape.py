"""Transforms that reshape rows: `sort_limit`, `rank`, `fill_domain` and `pivot`."""

from __future__ import annotations

import itertools
import math
import numbers
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from hari_lari_analytics.analysis.result import MatrixSpec, Result
from hari_lari_analytics.analysis.transforms import (
    Options,
    Requirements,
    TransformContext,
    TransformError,
    dimension_columns,
    need_columns,
    transform,
)
from hari_lari_analytics.semantic.registry import Dimension, Metric, Registry

ADDITIVE_FUNCS = ("sum", "count")  # an empty group is 0, not NULL (as in the compiler)


def row_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Boolean masks of the regular rows, the `Other (k)` rows and the total rows."""
    false = pd.Series(False, index=df.index)
    total = df["is_total"].astype(bool) if "is_total" in df else false
    other = df["is_other"].astype(bool) if "is_other" in df else false
    return ~total & ~other, other & ~total, total


# --- sort_limit ---------------------------------------------------------------------------------


class SortLimitOptions(Options):
    by: str  # a column of the result, such as net_sales or prior_net_sales
    desc: bool = True
    n: int | None = Field(default=None, ge=1)  # keep the first n rows


@transform("sort_limit", SortLimitOptions, shapes=("single", "rows"))
def sort_limit(result: Result, options: SortLimitOptions, ctx: TransformContext) -> Result:
    """Sort the rows by a column and keep the first n; `Other` and total rows stay at the end."""
    need_columns(result, "sort_limit", options.by)
    df = result.df
    regular, other, total = row_masks(df)
    # a stable sort, so ties keep the order of the query (by the dimension values); NULL last
    ordered = df[regular].sort_values(
        options.by, ascending=not options.desc, kind="stable", na_position="last"
    )
    if options.n is not None:
        ordered = ordered.iloc[: options.n]
    out = pd.concat([ordered, df[other], df[total]], ignore_index=True)
    return result.replace(out)


# --- rank ---------------------------------------------------------------------------------------


class RankOptions(Options):
    by: str
    desc: bool = True
    into: str = "rank"
    method: Literal["min", "dense", "first"] = "min"  # how ties rank: 1,1,3 | 1,1,2 | 1,2,3
    within: list[str] = []  # rank inside each group of these columns, such as the time bucket


@transform("rank", RankOptions, shapes=("rows", "series"))
def rank(result: Result, options: RankOptions, ctx: TransformContext) -> Result:
    """Add a rank column (1 = best) over the regular rows; Other and total rows get no rank."""
    need_columns(result, "rank", options.by, *options.within)
    if options.into in result.df.columns:
        raise TransformError(f"transform rank: the column {options.into} already exists")
    df = result.df.copy()
    regular, _, _ = row_masks(df)
    ranked = pd.Series(np.nan, index=df.index)
    rows = df[regular]
    if options.within:
        ranks = rows.groupby(options.within, dropna=False)[options.by].rank(
            method=options.method, ascending=not options.desc, na_option="keep"
        )
    else:
        ranks = rows[options.by].rank(
            method=options.method, ascending=not options.desc, na_option="keep"
        )
    ranked.loc[ranks.index] = ranks
    df[options.into] = ranked.astype("Int64")
    return result.replace(df)


# --- fill_domain --------------------------------------------------------------------------------


class FillDomainOptions(Options):
    # the dimensions to fill; default: every dimension of the result that has a domain to fill
    dimensions: list[str] | None = None
    values: dict[str, list] = {}  # the domain to use instead of the dimension's own


def _domain_values(dim: Dimension, existing: list, override: list | None) -> list | None:
    """The values a dimension should have, in order; None when it has no domain to fill."""
    if override is not None:
        return list(override)
    if dim.bins:
        return [b.label for b in dim.bin_list]
    if dim.domain == "all" and dim.values:
        return list(dim.values)
    if dim.domain == "span":
        numbers = [int(v) for v in existing if v is not None and not pd.isna(v)]
        return list(range(min(numbers), max(numbers) + 1)) if numbers else []
    return None


def _empty_value(metric: Metric):
    """What an empty group has for a metric: 0 for sums and counts, otherwise unknown."""
    if metric.kind in ("sql", "filtered") and any(
        a.func in ADDITIVE_FUNCS for a in metric.aggregates.values()
    ):
        return 0
    return pd.NaT if metric.format == "timestamp" else np.nan


def _clean(value):
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else value


@transform("fill_domain", FillDomainOptions, shapes=("rows",))
def fill_domain(result: Result, options: FillDomainOptions, ctx: TransformContext) -> Result:
    """Add the missing values of a dimension's domain (bins, all weekdays, the hours between the
    first and last) as rows with zeros, in the order of the domain.

    A filled row has zero sums and counts and no value for ratios; it has no comparison (the
    query dropped the groups without sales in the current window, so what the comparison window
    held for them is unknown). Its trading days come from the rows that share its weekday, or
    for a weekday nobody sold on, from the calendar."""
    meta, df = result.meta, result.df
    dims = dimension_columns(result)
    wanted = options.dimensions
    if wanted is None:
        wanted = [d for d in dims if d in options.values or _fillable(meta.dimensions[d])]
    unknown = [d for d in wanted if d not in dims]
    if unknown:
        raise TransformError(
            f"transform fill_domain: {', '.join(unknown)} is not a dimension of the result "
            f"({', '.join(dims) or 'none'})"
        )
    if not wanted or meta.empty:
        return result
    regular, other, total = row_masks(df)
    body = df[regular]

    domains: dict[str, list] = {}
    for d in wanted:
        dim = meta.dimensions[d]
        if dim.domain == "calendar" and d not in options.values:
            raise TransformError(
                f"transform fill_domain: {d} is a time dimension and the query already fills "
                "every bucket"
            )
        values = _domain_values(dim, body[d].tolist(), options.values.get(d))
        if values is None:
            raise TransformError(
                f"transform fill_domain: {d} has no domain to fill (domain none, no bins); "
                f"give the values: `values: {{{d}: [...]}}`"
            )
        domains[d] = values

    others = [d for d in dims if d not in wanted]
    contexts = list(
        dict.fromkeys(tuple(_clean(v) for v in row) for row in body[others].itertuples(index=False))
    )
    present = {tuple(_clean(v) for v in row) for row in body[dims].itertuples(index=False)}
    new_rows = []
    for combo in itertools.product(*(domains[d] for d in wanted)):
        for context in contexts:
            key = {
                **dict(zip(wanted, combo, strict=True)),
                **dict(zip(others, context, strict=True)),
            }
            if tuple(key[d] for d in dims) not in present:
                new_rows.append(_empty_row(result, key, body, ctx))
    if not new_rows:
        return result
    added = pd.DataFrame(new_rows, columns=df.columns)
    out = pd.concat([body, added], ignore_index=True)
    out = _sort_by_domains(out, result, domains, body)
    out = pd.concat([out, df[other], df[total]], ignore_index=True)
    return result.replace(out)


def _fillable(dim: Dimension) -> bool:
    return bool(dim.bins) or dim.domain in ("all", "span")


def _empty_row(result: Result, key: dict, body: pd.DataFrame, ctx: TransformContext) -> dict:
    meta, columns = result.meta, result.df.columns
    row: dict = {c: np.nan for c in columns}
    row |= key
    if "is_total" in row:
        row["is_total"] = False
    if "is_other" in row:
        row["is_other"] = False
    if "in_window" in row:
        row["in_window"] = True
    for column, metric in meta.metrics.items():
        if column in row:
            row[column] = 0 if metric.id != column else _empty_value(metric)
    if "trading_days" in columns:
        row["trading_days"], row["prior_trading_days"] = _trading_days(result, key, body, ctx)
    return {c: row[c] for c in columns}


def _trading_days(result: Result, key: dict, body: pd.DataFrame, ctx: TransformContext):
    meta = result.meta
    day_dims = [d for d in key if meta.dimensions[d].grain == "day"]
    if not day_dims:
        return meta.trading_days, meta.prior_trading_days
    for _, row in body.iterrows():  # a row with the same calendar values knows its trading days
        if all(_same(row[d], key[d]) for d in day_dims):
            return row["trading_days"], row.get("prior_trading_days", np.nan)
    if day_dims == ["weekday"] and ctx.calendar is not None and meta.window is not None:
        counts = []
        for window in (meta.window.current, meta.window.comparison):
            counts.append(
                np.nan
                if window is None
                else sum(
                    window.contains(d) and d.isoweekday() == key["weekday"]
                    for d in ctx.calendar.trading_dates
                )
            )
        return counts[0], counts[1] if meta.compare_ok else np.nan
    return np.nan, np.nan


def _same(a, b) -> bool:
    return (pd.isna(a) and pd.isna(b)) or a == b


def _sort_by_domains(
    out: pd.DataFrame, result: Result, domains: dict[str, list], body: pd.DataFrame
) -> pd.DataFrame:
    """Order the rows like the query does: by each dimension in turn, the filled ones in the order
    of their domain, the others by first appearance, which keeps a sort by a metric."""
    dims = dimension_columns(result)
    keys = pd.DataFrame(index=out.index)
    for d in dims:
        if d in domains:
            order = {v: i for i, v in enumerate(domains[d])}
            sort = result.meta.dimensions[d].sort
            if sort.by != "value":  # sorted by a metric: the existing rows first, then the domain
                seen = list(dict.fromkeys(body[d]))
                order = {v: i for i, v in enumerate([*seen, *domains[d]])}
            elif sort.desc:
                order = {v: -i for v, i in order.items()}
        else:
            order = {v: i for i, v in enumerate(dict.fromkeys(body[d]))}
        keys[d] = [order.get(_clean(v), math.inf) for v in out[d]]  # an empty value last
    return out.loc[keys.sort_values(dims, kind="stable").index].reset_index(drop=True)


# --- pivot --------------------------------------------------------------------------------------


class PivotOptions(Options):
    rows: str  # the dimension that becomes the rows
    columns: str  # the dimension that becomes the columns
    value: str | None = None  # the df column in the cells; default: the one metric of the result


def _pivot_requires(options: PivotOptions, registry: Registry) -> Requirements:
    return Requirements(dimensions=(options.rows, options.columns))


@transform(
    "pivot",
    PivotOptions,
    shapes=("rows", "series"),
    produces="matrix",
    requires=_pivot_requires,
)
def pivot(result: Result, options: PivotOptions, ctx: TransformContext) -> Result:
    """Turn two dimensions into a matrix: one row per value of `rows`, one column per value of
    `columns`, in the order of the result; columns of numbers or days are sorted (the hours of
    a weekday × hour matrix). Total rows are dropped."""
    meta, df = result.meta, result.df
    dims = dimension_columns(result)
    if options.rows == options.columns:
        raise TransformError("transform pivot: rows and columns must be different dimensions")
    for d in (options.rows, options.columns):
        if d not in dims:
            raise TransformError(
                f"transform pivot: {d} is not a dimension of the result ({', '.join(dims)})"
            )
    extra = [d for d in dims if d not in (options.rows, options.columns)]
    if extra:
        raise TransformError(
            f"transform pivot: the result also varies by {', '.join(extra)}; a cell would "
            "hold several rows (filter the query or drop the dimension)"
        )
    value = options.value
    if value is None:
        metrics = list(meta.value_metrics)
        if len(metrics) != 1:
            raise TransformError(
                "transform pivot: name the `value` column, the result has "
                f"{len(metrics)} metrics ({', '.join(metrics)})"
            )
        value = metrics[0]
    need_columns(result, "pivot", value)
    if not pd.api.types.is_numeric_dtype(df[value]):
        raise TransformError(f"transform pivot: the column {value} is not numeric")

    _, _, total = row_masks(df)
    body = df[~total]

    def keys(dim_id: str) -> list:
        label = meta.dimensions[dim_id].null_label or "(none)"
        return [label if pd.isna(v) else v for v in body[dim_id]]

    row_keys, col_keys = keys(options.rows), keys(options.columns)
    row_order = list(dict.fromkeys(row_keys))
    col_order = list(dict.fromkeys(col_keys))
    if all(isinstance(k, numbers.Real | date) for k in col_order):
        col_order.sort()  # hours, weekdays, buckets: in their order, not as they first appear
    cells = np.full((len(row_order), len(col_order)), np.nan)
    seen: set[tuple[int, int]] = set()
    row_pos = {k: i for i, k in enumerate(row_order)}
    col_pos = {k: i for i, k in enumerate(col_order)}
    for r, c, v in zip(row_keys, col_keys, body[value], strict=True):
        cell = (row_pos[r], col_pos[c])
        if cell in seen:
            raise TransformError(
                f"transform pivot: two rows for {options.rows}={r}, {options.columns}={c}"
            )
        seen.add(cell)
        cells[cell] = v
    out = pd.DataFrame(cells, columns=col_order)
    out.insert(0, options.rows, row_order)
    spec = MatrixSpec(options.rows, options.columns, value, tuple(col_order))
    return result.replace(out, shape="matrix", matrix=spec)
