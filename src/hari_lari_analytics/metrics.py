"""KPIs over cleaned sales lines, optionally grouped by any set of dimensions.

Orders are distinct receipts and therefore never additive across groups: always compute
them from the lines, never by summing grouped results.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MONEY = ["gross_sales", "discounts", "refunds", "net_sales"]
METRICS = [*MONEY, "orders", "aov", "items_sold", "first_sale_ts", "last_sale_ts"]


def _summary(df: pd.DataFrame) -> dict:
    payments = df.loc[~df["is_refund"] & df["counts_as_order"], "sale_ts"]
    orders = df.loc[df["counts_as_order"], "receipt_nr"].nunique()
    result = {column: int(df[column].sum()) for column in MONEY}
    result["orders"] = orders
    result["aov"] = result["net_sales"] / orders if orders else np.nan
    result["items_sold"] = int(df["quantity"].sum())
    result["first_sale_ts"] = payments.min() if len(payments) else pd.NaT
    result["last_sale_ts"] = payments.max() if len(payments) else pd.NaT
    return result


def compute_metrics(lines: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """One row per group (or a single row without grouping) with all metrics."""
    if not by:
        return pd.DataFrame([_summary(lines)], columns=METRICS)
    rows = [
        {**dict(zip(by, key if isinstance(key, tuple) else (key,), strict=True)), **_summary(g)}
        for key, g in lines.groupby(by, observed=True, dropna=False, sort=True)
    ]
    return pd.DataFrame(rows, columns=[*by, *METRICS])


def category_penetration(
    lines: pd.DataFrame, category: str = "adjusted_category", by: list[str] | None = None
) -> pd.DataFrame:
    """Share of orders (receipts) containing each category, within whatever `lines` covers.

    Returns columns: *by, category, orders_with_category, total_orders, penetration.
    """
    by = by or []
    valid = lines[lines["counts_as_order"]]
    with_category = (
        valid.groupby([*by, category], dropna=False)["receipt_nr"]
        .nunique()
        .rename("orders_with_category")
        .reset_index()
    )
    if by:
        totals = valid.groupby(by, dropna=False)["receipt_nr"].nunique().rename("total_orders")
        result = with_category.merge(totals.reset_index(), on=by)
    else:
        result = with_category.assign(total_orders=valid["receipt_nr"].nunique())
    result["penetration"] = result["orders_with_category"] / result["total_orders"]
    return result.sort_values([*by, "penetration"], ascending=[True] * len(by) + [False])
