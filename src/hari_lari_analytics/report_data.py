"""The figures behind one PDF report section, independent of how they are drawn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from hari_lari_analytics.metrics import compute_metrics, contributive_aov
from hari_lari_analytics.periods import Period

GRAIN_FREQ = {"sale_date": "D", "sale_week": "W-MON", "sale_month": "MS"}
GRAIN_NAME = {"sale_date": "day", "sale_week": "week", "sale_month": "month"}
GROWTH_NAME = {"sale_week": "week-over-week", "sale_month": "month-over-month"}

KPI_SPECS = [
    # key, label, is an IDR amount, an increase is good
    ("net_sales", "Net Sales", True, True),
    ("orders", "Orders", False, True),
    ("aov", "Avg Order Value", True, True),
    ("items_sold", "Items Sold", False, True),
    ("gross_sales", "Gross Sales", True, True),
    ("discounts", "Discounts", True, False),
    ("refunds", "Refunds", True, False),
]


def ratio_change(current: float, prior: float) -> float:
    if pd.isna(prior) or prior == 0:
        return np.nan
    return current / prior - 1


@dataclass
class Section:
    period: Period
    prior_ok: bool
    empty: bool
    kpis: pd.DataFrame
    times: dict
    trend: pd.DataFrame
    spark: pd.DataFrame  # sparkline points: per day for L14D, per 7-day block otherwise
    categories: pd.DataFrame
    category_total: pd.Series  # orders and penetration counted over all receipts, not summed
    items: pd.DataFrame
    hours: pd.DataFrame
    payments: pd.DataFrame
    discounts: pd.DataFrame
    trading_days: int


def _kpis(c: pd.Series, p: pd.Series, prior_ok: bool) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "key": key,
                "label": label,
                "is_idr": is_idr,
                "up_is_good": good,
                "current": c[key],
                "prior": p[key] if prior_ok else np.nan,
                "change": ratio_change(c[key], p[key]) if prior_ok else np.nan,
            }
            for key, label, is_idr, good in KPI_SPECS
        ]
    )


def add_growth(
    trend: pd.DataFrame, lines: pd.DataFrame, period: Period, first_date: date
) -> pd.DataFrame:
    """Week-over-week or month-over-month change of net sales and orders per bucket.

    The daily view gets no growth. The first bucket compares with the bucket just before the
    period, but only if that bucket lies fully within the sales history.
    """
    trend = trend.assign(net_sales_growth=np.nan, orders_growth=np.nan)
    if period.grain not in GROWTH_NAME:
        return trend
    start = trend["bucket"].iloc[0]
    before = start - pd.tseries.frequencies.to_offset(GRAIN_FREQ[period.grain])
    first = pd.Series({"net_sales": np.nan, "orders": np.nan})
    if before.date() >= first_date:
        window = lines[(lines["sale_ts"] >= before) & (lines["sale_ts"] < start)]
        first = compute_metrics(window).iloc[0]
    for key in ["net_sales", "orders"]:
        previous = pd.Series([first[key], *trend[key].iloc[:-1]], dtype=float)
        trend[f"{key}_growth"] = trend[key].to_numpy() / previous.replace(0, np.nan).to_numpy() - 1
    return trend


def _trend(cur, prior, lines, period: Period, prior_ok: bool, first_date: date) -> pd.DataFrame:
    freq = GRAIN_FREQ[period.grain]
    buckets = pd.date_range(period.start, period.end, freq=freq)
    prior_buckets = pd.date_range(period.prior_start, period.prior_end, freq=freq)
    by = compute_metrics(cur, [period.grain]).set_index(period.grain).reindex(buckets)
    by_prior = compute_metrics(prior, [period.grain]).set_index(period.grain).reindex(prior_buckets)
    aligned = prior_ok and len(prior_buckets) == len(buckets)
    trend = pd.DataFrame(
        {
            "bucket": buckets,
            "net_sales": by["net_sales"].fillna(0).to_numpy(),
            "orders": by["orders"].fillna(0).to_numpy(),
            "prior_net_sales": by_prior["net_sales"].fillna(0).to_numpy() if aligned else np.nan,
            "prior_orders": by_prior["orders"].fillna(0).to_numpy() if aligned else np.nan,
            "tick": {
                "sale_date": [f"{d:%a}\n{d:%d}" for d in buckets],
                "sale_week": [f"W{d.isocalendar().week:02d}\n{d:%d %b}" for d in buckets],
                "sale_month": [f"{d:%b}\n{d:%Y}" for d in buckets],
            }[period.grain],
        }
    )
    return add_growth(trend, lines, period, first_date)


def _spark(cur: pd.DataFrame, period: Period) -> pd.DataFrame:
    """Daily points for the daily view, otherwise 7-day blocks ending on the period's last day.

    Counting the blocks back from the end keeps the latest point a full week; leftover days at
    the start of the period are left out.
    """
    if period.grain == "sale_date":
        starts = pd.date_range(period.start, period.end, freq="D")
        block = cur["sale_date"]
    else:
        end = pd.Timestamp(period.end)
        n = ((end - pd.Timestamp(period.start)).days + 1) // 7
        starts = pd.date_range(end=end - pd.Timedelta(days=6), periods=n, freq="7D")
        days_back = (end - cur["sale_date"]).dt.days
        block = end - pd.Timedelta(days=6) - pd.to_timedelta(days_back // 7 * 7, unit="D")
    spark = compute_metrics(cur.assign(block=block), ["block"]).set_index("block").reindex(starts)
    return spark[["net_sales", "orders", "aov", "items_sold"]].astype(float).fillna(0)


CATEGORY_COLUMNS = [
    "net_sales",
    "items_sold",
    "orders",
    "share",
    "penetration",
    "category_aov",
    "contributive_aov",
]


def _with_prior(table: pd.DataFrame, prior: pd.DataFrame | pd.Series | None) -> pd.DataFrame:
    """Add the prior values and the changes; `prior` is None when there is no comparison."""

    def before(column: str):
        return np.nan if prior is None else prior[column]

    table["prior_penetration"] = before("penetration")
    table["prior_category_aov"] = before("category_aov")
    table["prior_contributive_aov"] = before("contributive_aov")
    table["delta_pp"] = (table["penetration"] - table["prior_penetration"]) * 100
    table["category_aov_change"] = table["category_aov"] / table["prior_category_aov"] - 1
    table["contributive_aov_delta"] = table["contributive_aov"] - table["prior_contributive_aov"]
    return table


def _categories(
    cur: pd.DataFrame, prior: pd.DataFrame, prior_ok: bool
) -> tuple[pd.DataFrame, pd.Series]:
    """One row per category, sorted by net sales, and the total over all categories.

    With a comparison, categories sold only in the prior period are kept with zero current
    sales, so that the changes in contributive AOV add up to the change in AOV.
    """
    key = "adjusted_category"
    cats = contributive_aov(cur).set_index(key)
    before = contributive_aov(prior).set_index(key) if prior_ok else None
    if before is not None:
        gone = before.index.difference(cats.index)
        if len(gone):
            zero = {"net_sales": 0, "items_sold": 0, "orders": 0, "penetration": 0.0}
            cats = pd.concat([cats, pd.DataFrame(zero, index=gone).rename_axis(key)])
            cats["contributive_aov"] = cats["contributive_aov"].fillna(0.0)
        before = before.reindex(cats.index).fillna({"penetration": 0.0, "contributive_aov": 0.0})
    total_net = cats["net_sales"].sum()
    cats["share"] = cats["net_sales"] / total_net if total_net else np.nan
    cats = _with_prior(cats[CATEGORY_COLUMNS], before)
    cats = cats.sort_values("net_sales", ascending=False, kind="stable").reset_index()

    c = compute_metrics(cur).iloc[0]
    total = pd.DataFrame(
        [
            {
                "net_sales": c["net_sales"],
                "items_sold": c["items_sold"],
                "orders": c["orders"],
                "share": 1.0 if total_net else np.nan,
                "penetration": 1.0 if c["orders"] else np.nan,
                "category_aov": c["aov"],
                "contributive_aov": c["aov"],
            }
        ]
    )
    if prior_ok:
        p = compute_metrics(prior).iloc[0]
        before_total = pd.Series(
            {
                "penetration": 1.0 if p["orders"] else np.nan,
                "category_aov": p["aov"],
                "contributive_aov": p["aov"],
            }
        )
    else:
        before_total = None
    total = _with_prior(total, before_total).iloc[0]
    total["delta_pp"] = np.nan
    return cats, total


def build_section(lines: pd.DataFrame, period: Period, first_date: date) -> Section:
    cur = lines[period.mask(lines["sale_ts"])]
    prior = lines[period.prior_mask(lines["sale_ts"])]
    prior_ok = period.prior_start >= first_date
    c = compute_metrics(cur).iloc[0]
    p = compute_metrics(prior).iloc[0]

    spark = _spark(cur, period)
    categories, category_total = _categories(cur, prior, prior_ok)

    total_net = cur["net_sales"].sum()
    items = compute_metrics(cur, ["adjusted_item_variant", "adjusted_category"])
    items = items.sort_values("net_sales", ascending=False).head(15).reset_index(drop=True)
    items["share"] = items["net_sales"] / total_net if total_net else np.nan

    valid = cur[cur["counts_as_order"]]
    trading_days = valid["sale_date"].nunique()
    per_hour = valid.groupby("hour_of_day")["receipt_nr"].nunique()
    if per_hour.empty:
        hours = pd.DataFrame(columns=["hour", "orders"])
    else:
        span = range(int(per_hour.index.min()), int(per_hour.index.max()) + 1)
        hours = (per_hour.reindex(span, fill_value=0) / trading_days).rename("orders")
        hours = hours.rename_axis("hour").reset_index()

    pay = compute_metrics(cur, ["payment_type"]).sort_values("net_sales", ascending=False)
    pay["share"] = pay["net_sales"] / total_net if total_net else np.nan
    disc = compute_metrics(
        cur.assign(code=cur["discount_code"].fillna("No discount")), ["code"]
    ).sort_values("gross_sales", ascending=False)
    disc["rate"] = disc["discounts"] / disc["gross_sales"].replace(0, np.nan)

    return Section(
        period=period,
        prior_ok=prior_ok,
        empty=cur.empty,
        kpis=_kpis(c, p, prior_ok),
        times={
            "first": c["first_sale_ts"],
            "last": c["last_sale_ts"],
            "prior_first": p["first_sale_ts"] if prior_ok else pd.NaT,
            "prior_last": p["last_sale_ts"] if prior_ok else pd.NaT,
        },
        trend=_trend(cur, prior, lines, period, prior_ok, first_date),
        spark=spark,
        categories=categories,
        category_total=category_total,
        items=items,
        hours=hours,
        payments=pay.reset_index(drop=True),
        discounts=disc.reset_index(drop=True),
        trading_days=trading_days,
    )
