"""`cumulative_share`: the running share of a metric, for Pareto curves and ABC classes."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hari_lari_analytics.analysis.result import Result
from hari_lari_analytics.analysis.transforms import (
    Options,
    Requirements,
    TransformContext,
    TransformError,
    dimension_columns,
    need_columns,
    transform,
)
from hari_lari_analytics.analysis.transforms.shape import row_masks
from hari_lari_analytics.semantic.registry import Registry


class CumulativeShareOptions(Options):
    metric: str  # a fully additive metric, such as net_sales or items_sold
    into: str = "cumulative_share"
    sort: bool = True  # sort the rows by the metric, largest first (a Pareto order)


def _requires(options: CumulativeShareOptions, registry: Registry) -> Requirements:
    return Requirements(metrics=(options.metric,), sums=(options.metric,))


@transform("cumulative_share", CumulativeShareOptions, shapes=("rows",), requires=_requires)
def cumulative_share(
    result: Result, options: CumulativeShareOptions, ctx: TransformContext
) -> Result:
    """Add the running share of a fully additive metric, largest rows first.

    The share is over the total row when the result has one (so a top 15 reads against all
    sales), otherwise over the rows. An `Other (k)` row comes after the ranked rows. Total rows
    get no value. A ratio cannot be accumulated, so only additive metrics are accepted: `full`
    ones always, `order_grain` ones (orders) only when every dimension has one value per order."""
    meta = result.meta
    spec = meta.value_metrics.get(options.metric)
    if spec is None:
        raise TransformError(
            f"transform cumulative_share: {options.metric} is not a metric of the result "
            f"({', '.join(meta.value_metrics)})"
        )
    if spec.additivity == "none":
        raise TransformError(
            f"transform cumulative_share: {options.metric} is not additive, so a running share "
            "would be wrong"
        )
    if spec.additivity == "order_grain":
        bad = [d for d in dimension_columns(result) if meta.dimensions[d].grain in ("item", "line")]
        if bad:
            raise TransformError(
                f"transform cumulative_share: {options.metric} counts orders, and a receipt can "
                f"fall into several groups of {', '.join(bad)}, so the shares would not add up"
            )
    need_columns(result, "cumulative_share", options.metric)
    if options.into in result.df.columns:
        raise TransformError(f"transform cumulative_share: the column {options.into} exists")

    df = result.df
    regular, other, total = row_masks(df)
    body = df[regular]
    if options.sort:
        body = body.sort_values(options.metric, ascending=False, kind="stable", na_position="last")
    ranked = pd.concat([body, df[other]])
    values = ranked[options.metric].fillna(0).astype(float)
    totals = df.loc[total, options.metric].dropna()
    denominator = float(totals.iloc[0]) if len(totals) else float(values.sum())
    shares = values.cumsum() / denominator if denominator else values * np.nan
    out = pd.concat([ranked.assign(**{options.into: shares}), df[total]], ignore_index=True)
    return result.replace(out)
