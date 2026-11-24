"""Transforms over time: `per_trading_day` and `growth_vs_previous`."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from hari_lari_analytics.reporting.registry import Metric, Registry
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.transforms import (
    Options,
    Requirements,
    TransformContext,
    TransformError,
    dimension_columns,
    need_columns,
    time_dimension,
    transform,
)
from hari_lari_analytics.reporting.windows import Grain


def _is_sum(metric: Metric) -> bool:
    """A metric that sums over trading days: an aggregate that adds up (not a ratio or a time)."""
    return (
        metric.kind in ("sql", "filtered")
        and metric.additivity in ("full", "order_grain")
        and metric.format != "timestamp"
    )


# --- per_trading_day ----------------------------------------------------------------------------


class PerTradingDayOptions(Options):
    metrics: list[str] | None = None  # default: every summing metric of the result


@transform("per_trading_day", PerTradingDayOptions, shapes=("single", "rows", "series"))
def per_trading_day(result: Result, options: PerTradingDayOptions, ctx: TransformContext) -> Result:
    """Divide the summing metrics by the trading days of each row, which the query counted per
    time bucket, per weekday (or other calendar value) or for the whole window; the prior values
    by the prior trading days, and the changes are recomputed.

    Ratios, relative metrics and the sample-size columns stay as they are. A row without trading
    days has no value."""
    meta, df = result.meta, result.df.copy()
    available = meta.value_metrics
    if options.metrics is None:
        chosen = [m for m, spec in available.items() if _is_sum(spec)]
    else:
        chosen = options.metrics
        for m in chosen:
            if m not in available:
                raise TransformError(
                    f"transform per_trading_day: {m} is not a metric of the result "
                    f"({', '.join(available)})"
                )
            if not _is_sum(available[m]):
                raise TransformError(
                    f"transform per_trading_day: {m} does not sum over trading days "
                    "(a ratio, or already per trading day)"
                )
    if not chosen:
        return result

    def days(column: str, scalar: int | None) -> pd.Series:
        values = df[column] if column in df else pd.Series(scalar, index=df.index, dtype=float)
        return values.astype(float).where(lambda s: s > 0)  # 0 or unknown: no value

    td, prior_td = (
        days("trading_days", meta.trading_days),
        days("prior_trading_days", meta.prior_trading_days),
    )
    specs = dict(meta.metrics)
    for m in chosen:
        df[m] = df[m] / td
        if f"prior_{m}" in df:
            df[f"prior_{m}"] = df[f"prior_{m}"] / prior_td
            df[f"{m}_change"] = df[m] / df[f"prior_{m}"].where(lambda s: s != 0) - 1
            df[f"{m}_delta"] = df[m] - df[f"prior_{m}"]
        spec = specs[m]
        specs[m] = dataclasses.replace(
            spec,
            label=f"{spec.label} per trading day",
            format="decimal" if spec.format == "count" else spec.format,
            additivity="none",
        )
    return result.replace(df, metrics=specs)


# --- growth_vs_previous -------------------------------------------------------------------------


class GrowthOptions(Options):
    metrics: list[str] | None = None  # default: every metric of the result except timestamps
    # a day-by-day growth is mostly the weekday pattern, so by default only these grains get one
    grains: list[Grain] = ["week", "month", "block_7d"]
    keep_before: bool = False  # keep the bucket(s) before the window, which `buckets_before` adds


def _growth_requires(options: GrowthOptions, registry: Registry) -> Requirements:
    return Requirements(buckets_before=1, metrics=tuple(options.metrics or ()))


@transform("growth_vs_previous", GrowthOptions, shapes=("series",), requires=_growth_requires)
def growth_vs_previous(result: Result, options: GrowthOptions, ctx: TransformContext) -> Result:
    """Add `<metric>_growth`: each bucket over the one before it, per group, as a ratio.

    The first bucket compares with the bucket before the window, which the query adds with
    `buckets_before` (only when that bucket lies fully within the sales history); without it the
    first bucket has no growth. A previous value of 0 gives no growth. That bucket is dropped
    from the result afterwards unless `keep_before` is set."""
    meta, df = result.meta, result.df.copy()
    time = time_dimension(result)
    if time is None:
        raise TransformError("transform growth_vs_previous: the result has no time dimension")
    available = meta.value_metrics
    if options.metrics is None:
        chosen = [m for m, spec in available.items() if spec.format != "timestamp"]
    else:
        chosen = options.metrics
        unknown = [m for m in chosen if m not in available]
        if unknown:
            raise TransformError(
                f"transform growth_vs_previous: {', '.join(unknown)} is not a metric of the "
                f"result ({', '.join(available)})"
            )
    need_columns(result, "growth_vs_previous", *chosen)

    if meta.grain in options.grains:
        # one group per member (and the bucket totals as a group); the window total is alone
        keys = [c for c in dimension_columns(result) if c != time]
        group = pd.DataFrame(
            {
                "_total": df["is_total"],
                "_no_bucket": df[time].isna(),
                **{f"_k_{c}": df[c] for c in keys},
            }
        )
        order = df.assign(_row=np.arange(len(df))).sort_values([time, "_row"], na_position="last")
        for m in chosen:
            frame = group.assign(_value=df[m]).loc[order.index]
            previous = frame.groupby(list(group.columns), dropna=False, sort=False)["_value"].shift(
                1
            )
            df[f"{m}_growth"] = df[m] / previous.where(lambda s: s != 0).reindex(df.index) - 1
    else:
        for m in chosen:
            df[f"{m}_growth"] = np.nan

    if "in_window" in df and not options.keep_before:
        keep = df["in_window"].astype(bool)
        df = df[keep].drop(columns="in_window").reset_index(drop=True)
        return result.replace(df, buckets=tuple(b for b in meta.buckets if b.ordinal >= 0))
    return result.replace(df)
