"""The sample-size rule: `small_n_mask` for the others, and the `suppress_small_n` transform."""

from __future__ import annotations

import pandas as pd

from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.transforms import (
    Options,
    TransformContext,
    TransformError,
    transform,
)


def small_n_mask(
    result: Result, metric: str, prior: bool = False, threshold: int | None = None
) -> pd.Series:
    """True for the rows whose sample is below the metric's `min_n` rule.

    The rule names counts (orders, or the orders with a drink); a row is small when any of its
    counts is below the threshold, which is the metric's own unless `threshold` replaces it. A
    count is that of the row's group in its own period (for an item: the orders that contain it).
    An unknown count (no comparison) is not small, since the value it belongs to is empty anyway."""
    spec = result.meta.metrics.get(metric)
    if spec is None:
        raise TransformError(f"{metric} is not a metric of the result")
    small = pd.Series(False, index=result.df.index)
    for count, own in spec.min_n.items():
        column = f"{'prior_' if prior else ''}n_{count}"
        if column not in result.df:
            raise TransformError(f"the sample-size column {column} of {metric} is missing")
        small |= (result.df[column] < (own if threshold is None else threshold)).fillna(False)
    return small


class SuppressSmallNOptions(Options):
    metrics: list[str] | None = None  # default: every metric of the result


@transform("suppress_small_n", SuppressSmallNOptions, shapes=("single", "rows", "series"))
def suppress_small_n(
    result: Result, options: SuppressSmallNOptions, ctx: TransformContext
) -> Result:
    """Blank the metrics of the rows below the sample-size rule, instead of greying them out.

    A value is blank when its own sample is small: the metric of the current period by the
    current counts, its prior by the prior counts, and a change, delta or growth when either
    side is small. The sample-size columns stay, so the reader sees how small the sample was."""
    meta, df = result.meta, result.df.copy()
    available = meta.value_metrics
    chosen = options.metrics if options.metrics is not None else list(available)
    unknown = [m for m in chosen if m not in available]
    if unknown:
        raise TransformError(
            f"transform suppress_small_n: {', '.join(unknown)} is not a metric of the result "
            f"({', '.join(available)})"
        )
    for m in chosen:
        small = small_n_mask(result, m)
        has_prior = f"prior_{m}" in df
        prior_small = small_n_mask(result, m, prior=True) if has_prior else small & False
        for column, mask in (
            (m, small),
            (f"prior_{m}", prior_small),
            (f"{m}_change", small | prior_small),
            (f"{m}_delta", small | prior_small),
            (f"{m}_growth", small),
        ):
            if column in df:
                df[column] = df[column].mask(mask)
    return result.replace(df)
