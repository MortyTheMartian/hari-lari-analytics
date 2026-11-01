"""PDF report: a cover page plus three pages per period (L14D, L6W, L3M)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.gridspec import GridSpec
from matplotlib.table import Table
from matplotlib.ticker import FuncFormatter

from hari_lari_analytics.metrics import category_penetration, compute_metrics
from hari_lari_analytics.periods import Period

A4_LANDSCAPE = (11.69, 8.27)
ACCENT = "#2f6f4f"
SECOND = "#d98c3a"
STRIPE = "#f3f3ee"
ROW_INCHES = 0.27
GRAIN_FREQ = {"sale_date": "D", "sale_week": "W-MON", "sale_month": "MS"}
GRAIN_NAME = {"sale_date": "day", "sale_week": "ISO week", "sale_month": "month"}


def idr(value: float) -> str:
    return "–" if pd.isna(value) else f"{value:,.0f}"


def pct(value: float) -> str:
    return "–" if pd.isna(value) else f"{value:.1%}"


def stamp(value) -> str:
    return "–" if pd.isna(value) else f"{value:%d %b %H:%M}"


def change(current: float, prior: float, available: bool) -> str:
    if not available or pd.isna(prior) or prior == 0:
        return "n/a"
    ratio = current / prior - 1
    return ">+999%" if ratio > 9.99 else f"{ratio:+.1%}"


def _compact(value: float, _pos=None) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


def _table(
    ax,
    header: list[str],
    rows: list[list[str]],
    widths: list[float],
    title: str = "",
    text_columns: int = 1,
):
    """Draw a striped table; the first `text_columns` are left-aligned, the rest right-aligned."""
    ax.axis("off")
    if title:
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color=ACCENT)
    if not rows:
        ax.text(0, 0.9, "No sales in this period.", fontsize=9, transform=ax.transAxes)
        return
    axes_inches = ax.get_position().height * ax.figure.get_figheight()
    height = min(ROW_INCHES / axes_inches, 0.98 / (len(rows) + 1))
    scale = 1 / sum(widths)
    table = Table(ax, loc="upper left")
    for r, values in enumerate([header, *rows]):
        for c, text in enumerate(values):
            loc = "left" if c < text_columns else "right"
            cell = table.add_cell(r, c, widths[c] * scale, height, text=text, loc=loc)
            cell.set_edgecolor("#dddddd")
            cell.set_linewidth(0.4)
            if r == 0:
                cell.set_facecolor(ACCENT)
                cell.get_text().set_color("white")
                cell.get_text().set_fontweight("bold")
            elif r % 2 == 0:
                cell.set_facecolor(STRIPE)
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    ax.add_table(table)


def _page(title: str, subtitle: str, footer: str) -> Figure:
    fig = Figure(figsize=A4_LANDSCAPE)
    fig.text(0.03, 0.955, title, fontsize=17, fontweight="bold", color=ACCENT)
    fig.text(0.03, 0.925, subtitle, fontsize=10, color="#555555")
    fig.text(0.03, 0.02, footer, fontsize=7.5, color="#888888")
    return fig


class PdfReport:
    def __init__(self, lines: pd.DataFrame, notes: list[str], sources: list[str]):
        self.lines = lines
        self.notes = notes
        self.sources = sources
        self.first_date: date = lines["sale_ts"].min().date()
        self.anchor: date = lines["sale_ts"].max().date()
        self.generated = datetime.now()
        self.page_no = 0

    def _footer(self) -> str:
        self.page_no += 1
        return (
            f"Hari Lari Cafe · Moka POS sales · data through {self.anchor:%d %b %Y} · "
            f"generated {self.generated:%d %b %Y %H:%M} · page {self.page_no}"
        )

    def build(self, path: Path, periods: list[Period]) -> None:
        with matplotlib.rc_context(sns.axes_style("whitegrid")), PdfPages(path) as pdf:
            pdf.savefig(self._cover(periods))
            for period in periods:
                cur = self.lines[period.mask(self.lines["sale_ts"])]
                prior = self.lines[period.prior_mask(self.lines["sale_ts"])]
                prior_ok = period.prior_start >= self.first_date
                for page in (self._overview, self._categories, self._details):
                    pdf.savefig(page(period, cur, prior, prior_ok))
            info = pdf.infodict()
            info["Title"] = f"Hari Lari sales report through {self.anchor:%d %b %Y}"

    def _subtitle(self, period: Period, prior_ok: bool) -> str:
        comparison = (
            f"compared with {period.prior_label()}"
            if prior_ok
            else f"no comparison: the prior period starts before the first sale "
            f"({self.first_date:%d %b %Y})"
        )
        partial = (
            f" · partial: data starts {self.first_date:%d %b %Y}"
            if period.start < self.first_date
            else ""
        )
        return f"{period.label()}{partial} · {comparison}"

    def _cover(self, periods: list[Period]) -> Figure:
        fig = _page(
            "Hari Lari Cafe · Sales Report",
            f"Sales history {self.first_date:%d %b %Y} – {self.anchor:%d %b %Y} · "
            f"last sale {self.lines['sale_ts'].max():%d %b %Y %H:%M}",
            self._footer(),
        )
        grid = GridSpec(
            3,
            1,
            figure=fig,
            top=0.86,
            bottom=0.08,
            left=0.03,
            right=0.97,
            height_ratios=[0.5, 0.8, 1.8],
            hspace=0.35,
        )

        total = compute_metrics(self.lines).iloc[0]
        _table(
            fig.add_subplot(grid[0]),
            [
                "",
                "Net Sales (IDR)",
                "Gross Sales",
                "Discounts",
                "Refunds",
                "Orders",
                "Avg Order Value",
                "Items Sold",
            ],
            [
                [
                    "All sales",
                    idr(total.net_sales),
                    idr(total.gross_sales),
                    idr(total.discounts),
                    idr(total.refunds),
                    idr(total.orders),
                    idr(total.aov),
                    idr(total.items_sold),
                ]
            ],
            [1.2, 1, 1, 1, 1, 0.8, 1, 0.8],
            "Full history",
        )
        _table(
            fig.add_subplot(grid[1]),
            ["Section", "Period", "Trend by", "Compared with"],
            [
                [
                    p.title,
                    p.label(),
                    GRAIN_NAME[p.grain],
                    p.prior_label()
                    if p.prior_start >= self.first_date
                    else "n/a (before first sale)",
                ]
                for p in periods
            ],
            [1.3, 1.6, 0.7, 1.6],
            "Report sections",
            text_columns=4,
        )
        ax = fig.add_subplot(grid[2])
        ax.axis("off")
        ax.set_title("Data notes", loc="left", fontsize=11, fontweight="bold", color=ACCENT)
        text = [f"Input files: {', '.join(self.sources)}"]
        text += self.notes or ["All item names matched the item library or the alias file."]
        text.append("Metric definitions: see METRICS.md in the repository.")
        ax.text(
            0,
            0.95,
            "\n".join(f"• {t}" for t in text),
            fontsize=8.5,
            va="top",
            wrap=True,
            transform=ax.transAxes,
        )
        return fig

    def _overview(self, period: Period, cur, prior, prior_ok) -> Figure:
        fig = _page(f"{period.title} · Overview", self._subtitle(period, prior_ok), self._footer())
        grid = GridSpec(
            1,
            2,
            figure=fig,
            top=0.86,
            bottom=0.12,
            left=0.03,
            right=0.95,
            width_ratios=[1, 1.6],
            wspace=0.12,
        )
        c = compute_metrics(cur).iloc[0]
        p = compute_metrics(prior).iloc[0]
        rows = [
            [
                label,
                fmt(c[key]),
                fmt(p[key]) if prior_ok else "n/a",
                change(c[key], p[key], prior_ok),
            ]
            for label, key, fmt in [
                ("Net Sales (IDR)", "net_sales", idr),
                ("Gross Sales (IDR)", "gross_sales", idr),
                ("Discounts (IDR)", "discounts", idr),
                ("Refunds (IDR)", "refunds", idr),
                ("Orders", "orders", idr),
                ("Avg Order Value (IDR)", "aov", idr),
                ("Items Sold", "items_sold", idr),
            ]
        ]
        for label, key in [("First Sale", "first_sale_ts"), ("Last Sale", "last_sale_ts")]:
            rows.append([label, stamp(c[key]), stamp(p[key]) if prior_ok else "n/a", ""])
        _table(
            fig.add_subplot(grid[0]),
            ["Metric", "Current", "Prior", "Change"],
            rows,
            [1.5, 1.1, 1.1, 0.85],
            "Key figures",
        )
        self._trend(fig.add_subplot(grid[1]), period, cur)
        return fig

    def _trend(self, ax, period: Period, cur: pd.DataFrame) -> None:
        buckets = pd.date_range(period.start, period.end, freq=GRAIN_FREQ[period.grain])
        by = compute_metrics(cur, [period.grain]).set_index(period.grain).reindex(buckets)
        x = np.arange(len(buckets))
        ax.bar(x, by["net_sales"].fillna(0), color=ACCENT, label="Net Sales (IDR)")
        ax.yaxis.set_major_formatter(FuncFormatter(_compact))
        orders_ax = ax.twinx()
        orders_ax.plot(x, by["orders"].fillna(0), color=SECOND, marker="o", label="Orders")
        orders_ax.set_ylim(bottom=0)
        orders_ax.grid(False)
        labels = {
            "sale_date": [f"{d:%a}\n{d:%d %b}" for d in buckets],
            "sale_week": [f"W{d.isocalendar().week:02d}\n{d:%d %b}" for d in buckets],
            "sale_month": [f"{d:%b %Y}" for d in buckets],
        }[period.grain]
        ax.set_xticks(x, labels, fontsize=8)
        ax.set_title(
            f"Net Sales and Orders by {GRAIN_NAME[period.grain]}",
            loc="left",
            fontsize=11,
            fontweight="bold",
            color=ACCENT,
        )
        handles = ax.get_legend_handles_labels()[0] + orders_ax.get_legend_handles_labels()[0]
        ax.legend(handles=handles, loc="upper left", fontsize=8, frameon=False)

    def _categories(self, period: Period, cur, prior, prior_ok) -> Figure:
        fig = _page(
            f"{period.title} · Categories", self._subtitle(period, prior_ok), self._footer()
        )
        grid = GridSpec(
            1,
            2,
            figure=fig,
            top=0.86,
            bottom=0.08,
            left=0.03,
            right=0.97,
            width_ratios=[1.75, 1],
            wspace=0.25,
        )
        table_ax, chart_ax = fig.add_subplot(grid[0]), fig.add_subplot(grid[1])
        if cur.empty:
            _table(table_ax, [], [], [], "Adjusted categories")
            chart_ax.axis("off")
            return fig

        by_cat = compute_metrics(cur, ["adjusted_category"])
        pen = category_penetration(cur).set_index("adjusted_category")["penetration"]
        prior_pen = category_penetration(prior).set_index("adjusted_category")["penetration"]
        by_cat["penetration"] = by_cat["adjusted_category"].map(pen)
        by_cat["prior_penetration"] = by_cat["adjusted_category"].map(prior_pen).fillna(0)
        by_cat = by_cat.sort_values("net_sales", ascending=False)
        total_net = by_cat["net_sales"].sum()
        rows = [
            [
                r.adjusted_category,
                idr(r.net_sales),
                pct(r.net_sales / total_net),
                idr(r.items_sold),
                idr(r.orders),
                pct(r.penetration),
                f"{(r.penetration - r.prior_penetration) * 100:+.1f} pp" if prior_ok else "n/a",
            ]
            for r in by_cat.itertuples()
        ]
        _table(
            table_ax,
            [
                "Adjusted Category",
                "Net Sales",
                "% of Net",
                "Items Sold",
                "Orders",
                "Penetration",
                "Δ Penetration",
            ],
            rows,
            [1.6, 1.1, 0.75, 0.8, 0.7, 0.85, 0.95],
            "Adjusted categories",
        )

        ordered = by_cat.sort_values("penetration", ascending=False)
        sns.barplot(data=ordered, y="adjusted_category", x="penetration", color=ACCENT, ax=chart_ax)
        chart_ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
        chart_ax.set(xlabel="Share of orders containing the category", ylabel="")
        chart_ax.tick_params(axis="y", labelsize=8)
        chart_ax.set_title(
            "% Category Order Penetration", loc="left", fontsize=11, fontweight="bold", color=ACCENT
        )
        return fig

    def _details(self, period: Period, cur, prior, prior_ok) -> Figure:
        fig = _page(
            f"{period.title} · Items, payments, discounts, hours",
            self._subtitle(period, prior_ok),
            self._footer(),
        )
        grid = GridSpec(
            3,
            2,
            figure=fig,
            top=0.86,
            bottom=0.07,
            left=0.03,
            right=0.97,
            width_ratios=[1.25, 1],
            height_ratios=[1.3, 0.8, 0.8],
            wspace=0.18,
            hspace=0.45,
        )
        total_net = cur["net_sales"].sum()

        items = compute_metrics(cur, ["adjusted_item_variant", "adjusted_category"])
        items = items.sort_values("net_sales", ascending=False).head(15)
        _table(
            fig.add_subplot(grid[:, 0]),
            ["Item-Variant (adjusted)", "Category", "Items Sold", "Net Sales", "% of Net"],
            [
                [
                    r.adjusted_item_variant,
                    r.adjusted_category,
                    idr(r.items_sold),
                    idr(r.net_sales),
                    pct(r.net_sales / total_net),
                ]
                for r in items.itertuples()
            ],
            [1.9, 1.3, 0.7, 0.9, 0.7],
            "Top 15 items by net sales",
            text_columns=2,
        )

        hour_ax = fig.add_subplot(grid[0, 1])
        valid = cur[cur["counts_as_order"]]
        hours = valid.groupby("hour_of_day")["receipt_nr"].nunique()
        days = valid["sale_date"].nunique()
        if not hours.empty:
            span = range(int(hours.index.min()), int(hours.index.max()) + 1)
            per_day = (hours.reindex(span, fill_value=0) / max(days, 1)).reset_index()
            per_day.columns = ["hour", "orders"]
            sns.barplot(data=per_day, x="hour", y="orders", color=SECOND, ax=hour_ax)
            hour_ax.set_xticks(range(len(span)), [f"{h:02d}" for h in span], fontsize=8)
        hour_ax.set(xlabel="Hour of day", ylabel="Orders per trading day")
        hour_ax.set_title(
            f"Orders by hour of day (avg over {days} trading days)",
            loc="left",
            fontsize=11,
            fontweight="bold",
            color=ACCENT,
        )

        pay = compute_metrics(cur, ["payment_type"]).sort_values("net_sales", ascending=False)
        _table(
            fig.add_subplot(grid[1, 1]),
            ["Payment Type", "Orders", "Net Sales", "% of Net"],
            [
                [r.payment_type, idr(r.orders), idr(r.net_sales), pct(r.net_sales / total_net)]
                for r in pay.itertuples()
            ],
            [1.3, 0.7, 1, 0.7],
            "Payment types",
        )

        disc = compute_metrics(
            cur.assign(code=cur["discount_code"].fillna("No discount")), ["code"]
        ).sort_values("gross_sales", ascending=False)
        _table(
            fig.add_subplot(grid[2, 1]),
            ["Discount Code", "Orders", "Gross Sales", "Discounts", "Discount Rate"],
            [
                [
                    r.code,
                    idr(r.orders),
                    idr(r.gross_sales),
                    idr(r.discounts),
                    pct(r.discounts / r.gross_sales if r.gross_sales else np.nan),
                ]
                for r in disc.itertuples()
            ],
            [1.2, 0.6, 1, 0.9, 1.05],
            "Discounts",
        )
        return fig


def write_pdf(
    path: Path, lines: pd.DataFrame, periods: list[Period], notes: list[str], sources: list[str]
) -> None:
    PdfReport(lines, notes, sources).build(path, periods)
