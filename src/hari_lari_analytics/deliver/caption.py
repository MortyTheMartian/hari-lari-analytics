"""The caption under the weekly cover photo in Telegram."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.report_data import ratio_change

UP, DOWN = "▲", "▼"


def date_range(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{start:%-d %b %Y} – {end:%-d %b %Y}"
    if start.month != end.month:
        return f"{start:%-d %b} – {end:%-d %b %Y}"
    return f"{start.day}–{end:%-d %b %Y}"


def _idr(value: float) -> str:
    return f"IDR {value:,.0f}"


def _change(current: float, prior: float) -> str:
    change = ratio_change(current, prior)
    if pd.isna(change):
        return "no prior week"
    if round(change * 100, 1) == 0:
        return "unchanged"
    return f"{UP if change > 0 else DOWN} {abs(change) * 100:.1f}%"


def build_caption(lines: pd.DataFrame, anchor: date, drive_url: str | None = None) -> str:
    """Week, net sales, orders and AOV against the week before, the last sale, a Drive link.

    The week is the 7 days ending on `anchor` (a Sunday in the normal run). `lines` are the
    cleaned sales lines, including at least the prior week.
    """
    start = anchor - timedelta(days=6)
    prior_start = start - timedelta(days=7)
    ts = lines["sale_ts"]
    current = lines[(ts >= pd.Timestamp(start)) & (ts < pd.Timestamp(anchor + timedelta(days=1)))]
    prior = lines[(ts >= pd.Timestamp(prior_start)) & (ts < pd.Timestamp(start))]

    head = [
        "Hari Lari weekly sales report",
        f"Week {anchor.isocalendar().week} · {date_range(start, anchor)}",
        "",
    ]
    if current.empty:
        body = ["No sales in this week."]
    else:
        c = compute_metrics(current).iloc[0]
        p = compute_metrics(prior).iloc[0] if not prior.empty else None

        def row(label: str, key: str, text: str) -> str:
            prior_value = p[key] if p is not None else float("nan")
            return f"{label}: {text} ({_change(c[key], prior_value)})"

        body = [
            row("Net sales", "net_sales", _idr(c.net_sales)),
            row("Orders", "orders", f"{c.orders:,}"),
            row("Avg order value", "aov", _idr(c.aov)),
            f"Change vs previous week ({date_range(prior_start, start - timedelta(days=1))})",
        ]
    foot = []
    last_sale = compute_metrics(lines).iloc[0].last_sale_ts
    if pd.notna(last_sale):
        foot.append(f"Last sale: {last_sale:%a %-d %b %Y, %H:%M}")
    if drive_url:
        foot.append(f"PDF and Excel lines: {drive_url}")
    return "\n".join([*head, *body, *([""] if foot else []), *foot])
