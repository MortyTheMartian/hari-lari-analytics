"""Evaluate registry metrics with plain SQL, for the tests of the semantic layer.

A stand-in for the query compiler (plan 7.9): one dimension at most, a total row through
grouping sets, and each derived or relative metric in its own step.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pandas as pd

from hari_lari_analytics.semantic.registry import Registry


def evaluate(
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    model: str,
    metrics: list[str],
    by: str | None = None,
    start: date | None = None,
    end: date | None = None,
    trading_days: int | None = None,
) -> pd.DataFrame:
    """One row per value of `by` plus the total row (`is_total`), or one row without `by`."""
    plan = registry.plan(metrics, model)
    if by:
        column = registry.dimension(by).column_on(model)
        head = f"{column} as {by}, grouping({column}) = 1 as is_total"
        group = f"group by grouping sets (({column}), ())"
    else:
        head, group = "true as is_total", ""
    where, params = "true", []
    if start is not None:
        where = "sale_ts >= ? and sale_ts < ?"
        params = [start, end + timedelta(days=1)]
    aggregates = ", ".join(f"{registry.aggregate(m, model)} as {m}" for m in plan.aggregates)
    steps = [f"s0 as (select {head}, {aggregates} from {model} where {where} {group})"]
    for i, metric_id in enumerate(plan.computed, start=1):
        expression = registry.expression(
            metric_id,
            ref=lambda m: f"s.{m}",
            total=lambda m: f"t.{m}",
            trading_days="null" if trading_days is None else str(trading_days),
        )
        source = f"s{i - 1} as s"
        if registry.metric(metric_id).totals:
            source += f", (select * from s{i - 1} where is_total) as t"
        steps.append(f"s{i} as (select s.*, {expression} as {metric_id} from {source})")
    sql = f"with {', '.join(steps)} select * from s{len(steps) - 1}"
    return con.execute(sql, params).df()
