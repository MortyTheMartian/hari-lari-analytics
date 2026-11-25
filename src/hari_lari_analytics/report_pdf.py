"""PDF report: a cover page plus three pages per period (L14D, L6W, L3M), drawn with matplotlib.

All colours come from a `Palette`; pass another one to `PdfReport` to re-theme the report. The
palette, the number formats and the drawing building blocks live in `reporting/formats.py` and
`reporting/visuals/` (plan 8.1), which the component report uses too.
"""

from __future__ import annotations

import textwrap
from datetime import date, datetime
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.text import Text
from matplotlib.ticker import FuncFormatter
from plottable import ColumnDefinition, Table

from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import Period
from hari_lari_analytics.project import project
from hari_lari_analytics.report_data import GRAIN_NAME, GROWTH_NAME, Section, build_section
from hari_lari_analytics.reporting.formats import (  # noqa: F401  (imported from here too)
    change_text,
    compact,
    headline,
    idr,
    pct,
    stamp,
)
from hari_lari_analytics.reporting.visuals import base
from hari_lari_analytics.reporting.visuals.theme import (  # noqa: F401
    DAYLIGHT,
    FONTS,
    H,
    Palette,
    Theme,
    W,
)

MAX_PAYMENT_TYPES = 4
COVER_PNG_DPI = 150  # 1753 x 1240 px: sharp on a phone, far below Telegram's photo limits


class PdfReport:
    def __init__(
        self,
        lines: pd.DataFrame,
        notes: list[str],
        sources: list[str],
        palette: Palette = DAYLIGHT,
        anchor: date | None = None,
    ):
        self.lines = lines
        self.notes = notes
        self.sources = sources
        self.p = palette
        self.first_date: date = lines["sale_ts"].min().date()
        self.anchor: date = anchor or lines["sale_ts"].max().date()
        self.generated = datetime.now()

    def build(self, path: Path, periods: list[Period], cover_png: Path | None = None) -> None:
        sections = [build_section(self.lines, period, self.first_date) for period in periods]
        with matplotlib.rc_context(self._rc()), PdfPages(path) as pdf:
            cover = self._cover(sections)
            pdf.savefig(cover)
            if cover_png is not None:
                cover.savefig(cover_png, dpi=COVER_PNG_DPI)
            full = [self._overview, self._categories, self._details]
            for s in sections:
                for page in [self._empty] if s.empty else full:
                    pdf.savefig(page(s))
            info = pdf.infodict()
            info["Title"] = f"Hari Lari sales report through {self.anchor:%d %b %Y}"

    # Building blocks

    def _rc(self) -> dict:
        return Theme(palette=self.p).rc()

    def _color(self, value: float, up_is_good: bool = True) -> str:
        return base.change_color(self.p, value, up_is_good)

    def _card(self, fig: Figure, x, y, w, h, title: str = "", hint: str = "") -> None:
        base.card(fig, self.p, x, y, w, h, title, hint)

    def _chips(self, fig: Figure, chips: list[tuple[str, str]]) -> None:
        base.chips(fig, self.p, chips)

    def _page(self, kicker: str, title: str, chips: list[tuple[str, str]], note="") -> Figure:
        fig = Figure(figsize=(W, H))
        fig.text(
            0.035, 0.935, kicker.upper(), fontsize=7.5, color=self.p.primary, fontweight="semibold"
        )
        fig.text(0.035, 0.895, title, fontsize=21, fontweight="bold")
        self._chips(fig, chips)
        if note:
            fig.text(0.965, 0.872, note, fontsize=7, ha="right", color=self.p.muted)
        fig.text(
            0.035,
            0.025,
            f"Hari Lari Cafe · Moka POS sales · data through {self.anchor:%d %b %Y} · "
            f"generated {self.generated:%d %b %Y %H:%M}",
            fontsize=7,
            color=self.p.muted,
        )
        return fig

    def _section_page(self, s: Section, title: str) -> Figure:
        chips = [(s.period.title, "fill"), (s.period.label(), "line")]
        if s.prior_ok:
            return self._page(
                "Hari Lari Cafe", title, [*chips, (f"vs {s.period.prior_label()}", "muted")]
            )
        note = f"The prior period starts before the first sale ({self.first_date:%d %b %Y})."
        return self._page("Hari Lari Cafe", title, [*chips, ("no comparison", "muted")], note)

    def _after(self, fig: Figure, anchor: Text, text: str, gap: float = 4, **kw) -> None:
        base.after(fig, anchor, text, gap, **kw)

    def _sparkline(self, fig: Figure, rect, values: np.ndarray) -> None:
        base.sparkline(fig, self.p, rect, values)

    def _table(self, ax, df: pd.DataFrame, defs: list[ColumnDefinition], index: str, size=8.0):
        return base.table(ax, self.p, df, defs, index, size)

    def _bar_column(self, name: str, title: str, vmax: float, width: float) -> ColumnDefinition:
        return base.bar_column(self.p, name, title, vmax, width)

    # Pages

    def _cover(self, sections: list[Section]) -> Figure:
        last_sale = self.lines["sale_ts"].max()
        fig = self._page(
            "Hari Lari Cafe · Moka POS",
            "Sales report",
            [
                ("Full history", "fill"),
                (f"{self.first_date:%d %b %Y} – {self.anchor:%d %b %Y}", "line"),
            ],
            f"Last sale {last_sale:%d %b %Y %H:%M}",
        )
        t = compute_metrics(self.lines).iloc[0]
        code = project().currency.code
        x0, w, gap, y, h = 0.035, 0.2225, 0.0133, 0.70, 0.13
        for i, (label, value, unit) in enumerate(
            [
                ("Net sales · full history", headline(t.net_sales, True), code),
                ("Orders", headline(t.orders, False), ""),
                ("Avg order value", headline(t.aov, True), code),
                ("Items sold", headline(t.items_sold, False), ""),
            ]
        ):
            x = x0 + i * (w + gap)
            self._card(fig, x, y, w, h)
            fig.text(
                x + 0.014,
                y + h - 0.03,
                label.upper(),
                fontsize=7,
                color=self.p.muted,
                fontweight="semibold",
            )
            value_text = fig.text(x + 0.014, y + 0.03, value, fontsize=24, fontweight="bold")
            self._after(fig, value_text, unit, fontsize=8.5, color=self.p.muted)
        fig.text(
            0.035,
            0.665,
            f"Gross {idr(t.gross_sales)} · discounts {idr(t.discounts)} · "
            f"refunds {idr(t.refunds)} {code}",
            fontsize=8,
            color=self.p.muted,
        )

        w3 = (0.93 - 2 * gap) / 3
        for i, s in enumerate(sections):
            x, y, h = 0.035 + i * (w3 + gap), 0.27, 0.355
            self._card(fig, x, y, w3, h)
            fig.text(x + 0.016, y + h - 0.036, s.period.title, fontsize=12, fontweight="bold")
            fig.text(x + 0.016, y + h - 0.06, s.period.label(), fontsize=8, color=self.p.muted)
            fig.text(
                x + 0.016,
                y + h - 0.08,
                f"by {GRAIN_NAME[s.period.grain]} · "
                + (f"vs {s.period.prior_label()}" if s.prior_ok else "no comparison"),
                fontsize=7.5,
                color=self.p.muted,
            )
            if s.empty:
                fig.text(x + 0.016, y + 0.17, "No sales in this period", fontsize=11)
                continue
            kpis = s.kpis.set_index("key")
            for j, key in enumerate(["net_sales", "orders"]):
                r = kpis.loc[key]
                ky = y + 0.17 - j * 0.07
                fig.text(
                    x + 0.016,
                    ky + 0.03,
                    r.label.upper(),
                    fontsize=6.8,
                    color=self.p.muted,
                    fontweight="semibold",
                )
                value_text = fig.text(
                    x + 0.016, ky, headline(r.current, r.is_idr), fontsize=17, fontweight="bold"
                )
                if s.prior_ok:
                    self._after(
                        fig,
                        value_text,
                        change_text(r.change),
                        gap=6,
                        fontsize=8,
                        color=self._color(r.change, r.up_is_good),
                        fontweight="semibold",
                    )
            self._sparkline(
                fig, (x + 0.016, y + 0.02, w3 - 0.032, 0.05), s.spark["net_sales"].to_numpy()
            )

        self._card(fig, 0.035, 0.07, 0.93, 0.18, "Data notes")
        notes = [f"Input files: {', '.join(self.sources)}"]
        notes += self.notes or ["All item names matched the item library or the alias file."]
        notes.append("Metric definitions: see METRICS.md in the repository.")
        if len(notes) > 5:
            notes = [*notes[:4], f"… and {len(notes) - 4} more notes in the run output."]
        for i, note in enumerate(notes):
            fig.text(
                0.05,
                0.19 - i * 0.025,
                "•  " + textwrap.shorten(note, 190, placeholder=" …"),
                fontsize=8,
                color=self.p.muted,
            )
        return fig

    def _empty(self, s: Section) -> Figure:
        fig = self._section_page(s, "Overview")
        self._card(fig, 0.035, 0.07, 0.93, 0.78, "No sales in this period")
        return fig

    def _overview(self, s: Section) -> Figure:
        fig = self._section_page(s, "Overview")
        self._kpi_cards(fig, s)
        self._trend(fig, s, (0.035, 0.07, 0.6, 0.6))
        self._key_figures(fig, s, (0.648, 0.07, 0.317, 0.6))
        return fig

    def _kpi_cards(self, fig: Figure, s: Section) -> None:
        x0, w, gap, y, h = 0.035, 0.2225, 0.0133, 0.70, 0.155
        for i, r in enumerate(s.kpis.head(4).itertuples()):
            x = x0 + i * (w + gap)
            self._card(fig, x, y, w, h)
            fig.text(
                x + 0.014,
                y + h - 0.03,
                r.label.upper(),
                fontsize=7,
                color=self.p.muted,
                fontweight="semibold",
            )
            fig.text(
                x + 0.014, y + 0.052, headline(r.current, r.is_idr), fontsize=22, fontweight="bold"
            )
            if s.prior_ok:
                change = fig.text(
                    x + 0.014,
                    y + 0.022,
                    change_text(r.change),
                    fontsize=8,
                    color=self._color(r.change, r.up_is_good),
                    fontweight="semibold",
                )
                self._after(
                    fig, change, f"prior {compact(r.prior)}", gap=8, fontsize=8, color=self.p.muted
                )
            else:
                fig.text(x + 0.014, y + 0.022, "no prior period", fontsize=8, color=self.p.muted)
            self._sparkline(
                fig, (x + w * 0.58, y + 0.045, w * 0.37, 0.06), s.spark[r.key].to_numpy()
            )

    def _growth_label(self, ax, x, y, value: float, offset: float, fontsize: float) -> None:
        ax.annotate(
            change_text(value, digits=0),
            (x, y),
            xytext=(0, offset),
            textcoords="offset points",
            ha="center",
            fontsize=fontsize,
            color=self._color(value),
            fontweight="semibold",
        )

    def _trend(self, fig: Figure, s: Section, rect) -> None:
        x, y, w, h = rect
        growth = s.period.grain in GROWTH_NAME
        hint = f"{project().currency.code} · " + (
            "ticks = prior period, same position" if s.prior_ok else "no prior period available"
        )
        if growth:
            hint += f" · ▲▼ {GROWTH_NAME[s.period.grain]} change"
        self._card(fig, x, y, w, h, f"Net sales & orders by {GRAIN_NAME[s.period.grain]}", hint)
        t = s.trend
        idx = np.arange(len(t))
        width = 0.62 if len(t) > 6 else 0.5

        ax = fig.add_axes((x + 0.045, y + 0.16, w - 0.06, h - 0.25))
        ax.bar(idx, t["net_sales"], width=width, color=self.p.primary, edgecolor="none")
        top = t["net_sales"].max()
        if s.prior_ok:
            ax.hlines(
                t["prior_net_sales"],
                idx - width * 0.62,
                idx + width * 0.62,
                color=self.p.secondary,
                lw=1.8,
            )
            top = max(top, t["prior_net_sales"].max())
        for i, v in enumerate(t["net_sales"]):
            ax.annotate(
                compact(v),
                (i, v),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=7.5,
                fontweight="medium",
            )
            if growth:
                self._growth_label(ax, i, v, t["net_sales_growth"].iloc[i], 13, 7)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: compact(v)))
        ax.grid(axis="y", color=self.p.grid, lw=0.6)
        ax.set_axisbelow(True)
        ax.set_xticks(idx, t["tick"])
        ax.set_xlim(-0.6, len(t) - 0.4)
        ax.set_ylim(0, max(top, 1) * (1.2 if growth else 1.12))

        orders = fig.add_axes((x + 0.045, y + 0.035, w - 0.06, 0.06), sharex=ax)
        if s.prior_ok:
            orders.plot(idx, t["prior_orders"], color=self.p.secondary, lw=1, ls=(0, (3, 2)))
        orders.plot(
            idx,
            t["orders"],
            color=self.p.text,
            lw=1.3,
            marker="o",
            ms=3.5,
            mfc=self.p.card,
            mew=1.2,
        )
        for i, v in enumerate(t["orders"]):
            orders.annotate(
                f"{v:,.0f}",
                (i, v),
                xytext=(0, 5),
                textcoords="offset points",
                ha="center",
                fontsize=6.5,
                color=self.p.muted,
            )
            if growth:
                self._growth_label(orders, i, v, t["orders_growth"].iloc[i], 13, 6.5)
        orders.axis("off")
        orders.set_ylim(0, max(t["orders"].max(), 1) * 1.5)
        fig.text(x + 0.014, y + 0.1, "Orders", fontsize=7.5, color=self.p.muted)

    def _key_figures(self, fig: Figure, s: Section, rect) -> None:
        x, y, w, h = rect
        hint = f"Amounts in {project().currency.code} · current vs prior period"
        self._card(fig, x, y, w, h, "Key figures", hint)
        df = pd.DataFrame(
            {
                "Metric": s.kpis["label"],
                "Current": s.kpis["current"].map(idr),
                "Prior": s.kpis["prior"].map(idr) if s.prior_ok else "n/a",
                "Change": s.kpis["change"].map(change_text),
            }
        )
        for label, cur, prior in [
            ("First sale", s.times["first"], s.times["prior_first"]),
            ("Last sale", s.times["last"], s.times["prior_last"]),
        ]:
            df.loc[len(df)] = [label, stamp(cur), stamp(prior) if s.prior_ok else "n/a", ""]
        ax = fig.add_axes((x + 0.012, y + 0.03, w - 0.024, h - 0.11))
        table = self._table(
            ax,
            df,
            [
                ColumnDefinition("Metric", title="METRIC", width=1.25, textprops={"ha": "left"}),
                ColumnDefinition(
                    "Current",
                    title="CURRENT",
                    width=1.1,
                    textprops={"ha": "right", "weight": "bold"},
                ),
                ColumnDefinition(
                    "Prior",
                    title="PRIOR",
                    width=1.0,
                    textprops={"ha": "right", "color": self.p.muted},
                ),
                ColumnDefinition(
                    "Change",
                    title="CHANGE",
                    width=0.85,
                    textprops={"ha": "right", "weight": "semibold"},
                ),
            ],
            "Metric",
        )
        for i, r in enumerate(s.kpis.itertuples()):
            table.cells[(i, 3)].text.set_color(self._color(r.change, r.up_is_good))

    def _delta_column(self, name: str, group: str, fmt) -> ColumnDefinition:
        return ColumnDefinition(
            name,
            title="Δ",
            group=group,
            width=0.75,
            textprops={"ha": "right", "weight": "semibold"},
            formatter=lambda v: "n/a" if pd.isna(v) else fmt(v),
            text_cmap=self._color,
        )

    def _categories(self, s: Section) -> Figure:
        fig = self._section_page(s, "Categories")
        hint = (
            "Sorted by net sales · AOV = Σ contributive AOV = Σ penetration × category AOV · "
            "a category's share of AOV equals its % of net"
            + (" · Δ vs prior period" if s.prior_ok else "")
        )
        self._card(fig, 0.035, 0.07, 0.93, 0.78, "Adjusted categories & contributive AOV", hint)
        c = pd.concat([s.categories, s.category_total.to_frame().T], ignore_index=True)
        c.loc[len(c) - 1, "adjusted_category"] = "Total"
        no_prior = pd.Series(np.nan, index=c.index)
        df = pd.DataFrame(
            {
                "Category": c["adjusted_category"],
                "Net Sales": c["net_sales"].map(idr),
                "share": c["share"].fillna(0).astype(float),
                "Items": c["items_sold"].map(idr),
                "Orders": c["orders"].map(idr),
                "Penetration": c["penetration"].map(pct),
                "pen_delta": c["delta_pp"] if s.prior_ok else no_prior,
                "Category AOV": c["category_aov"].map(idr),
                "aov_change": c["category_aov_change"] if s.prior_ok else no_prior,
                "Contributive AOV": c["contributive_aov"].map(idr),
                "contrib_delta": c["contributive_aov_delta"] if s.prior_ok else no_prior,
            }
        ).astype({"pen_delta": float, "aov_change": float, "contrib_delta": float})
        n = len(df)
        row = min(0.048, 0.56 / (n + 1))
        top = 0.745
        ax = fig.add_axes((0.047, top - row * (n + 1), 0.906, row * (n + 1)))
        table = self._table(
            ax,
            df,
            [
                ColumnDefinition("Category", title="CATEGORY", width=1.4, textprops={"ha": "left"}),
                ColumnDefinition(
                    "Net Sales",
                    title="NET SALES",
                    width=0.95,
                    textprops={"ha": "right", "weight": "bold"},
                ),
                self._bar_column("share", "% OF NET", df["share"].iloc[:-1].max(), 1.2),
                ColumnDefinition("Items", title="ITEMS", width=0.55, textprops={"ha": "right"}),
                ColumnDefinition("Orders", title="ORDERS", width=0.6, textprops={"ha": "right"}),
                ColumnDefinition(
                    "Penetration",
                    title="SHARE",
                    group="ORDER PENETRATION",
                    width=0.7,
                    textprops={"ha": "right"},
                ),
                self._delta_column("pen_delta", "ORDER PENETRATION", lambda v: f"{v:+.1f} pp"),
                ColumnDefinition(
                    "Category AOV",
                    title=project().currency.code,
                    group="CATEGORY AOV",
                    width=0.75,
                    textprops={"ha": "right"},
                ),
                self._delta_column(
                    "aov_change", "CATEGORY AOV", lambda v: ">+999%" if v > 9.99 else f"{v:+.1%}"
                ),
                ColumnDefinition(
                    "Contributive AOV",
                    title=project().currency.code,
                    group="CONTRIBUTIVE AOV",
                    width=0.75,
                    textprops={"ha": "right", "weight": "bold"},
                ),
                self._delta_column("contrib_delta", "CONTRIBUTIVE AOV", lambda v: f"{v:+,.0f}"),
            ],
            "Category",
            size=8.5,
        )
        self._style_total_row(table, n - 1, df.columns.get_loc("share"), s.prior_ok)
        fig.text(
            0.05,
            0.09,
            "Reading a change in contributive AOV: a moving penetration means the category is in "
            "more or fewer orders (an inclusion question);\na moving category AOV means orders "
            "with the category spend more or less on it (a pricing or basket-size question).\n"
            "Orders are counted per receipt, so they don't add up across categories: the total "
            "is all receipts, and its penetration is 100%. Net sales, items and contributive AOV "
            "add up.",
            fontsize=7.5,
            color=self.p.muted,
            linespacing=1.6,
        )
        return fig

    def _style_total_row(self, table: Table, idx: int, share_col: int, prior_ok: bool) -> None:
        """Bold total row below a solid line; its share is written out instead of drawn."""
        for cell in table.rows[idx].cells:
            if hasattr(cell, "text"):
                cell.text.set_fontweight("bold")
        pen_delta = table.cells[(idx, table.column_names.index("pen_delta"))]
        pen_delta.text.set_text("" if prior_ok else "n/a")
        x0, x1 = table.rows[idx].get_xrange()
        table.ax.plot([x0, x1], [idx, idx], color=self.p.muted, lw=0.8)
        sub = table.subplots[(idx, share_col)]
        sub.cla()
        sub.set_xlim(0, 1)
        sub.set_ylim(0, 1)
        sub.axis("off")
        sub.text(0, 0.5, "100.0%", va="center", fontsize=7.5, fontweight="bold", color=self.p.text)

    def _penetration_chart(self, fig: Figure, s: Section, rect=(0.607, 0.07, 0.358, 0.78)) -> None:
        """Order penetration per category as a dot plot; not on any page at the moment."""
        x, y, w, h = rect
        c = s.categories
        hint = "Share of orders containing the category" + (
            " · ○ prior period" if s.prior_ok else ""
        )
        self._card(fig, x, y, w, h, "Order penetration", hint)
        p = c.sort_values("penetration").fillna({"penetration": 0, "prior_penetration": 0})
        ax = fig.add_axes((x + 0.103, y + 0.05, w - 0.128, h - 0.15))
        yy = np.arange(len(p))
        if s.prior_ok:
            ax.hlines(yy, p["prior_penetration"], p["penetration"], color=self.p.grid, lw=3)
            ax.scatter(
                p["prior_penetration"],
                yy,
                s=26,
                facecolor=self.p.card,
                edgecolor=self.p.muted,
                lw=1.1,
                zorder=3,
            )
        colors = [self._color(d) if s.prior_ok else self.p.primary for d in p["delta_pp"]]
        ax.scatter(p["penetration"], yy, s=46, color=colors, zorder=4)
        for i, r in enumerate(p.itertuples()):
            right = max(r.penetration, r.prior_penetration) if s.prior_ok else r.penetration
            ax.text(
                right + 0.02,
                i,
                pct(r.penetration, 0),
                va="center",
                fontsize=8,
                fontweight="semibold",
            )
        ax.set_yticks(yy, p["adjusted_category"], color=self.p.text, fontsize=8)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
        ax.grid(axis="x", color=self.p.grid, lw=0.6)
        ax.spines["bottom"].set_visible(False)
        ax.set_xlim(0, max(p[["penetration", "prior_penetration"]].max().max(), 0.1) * 1.18)
        ax.set_ylim(-0.6, len(p) - 0.4)

    def _details(self, s: Section) -> Figure:
        fig = self._section_page(s, "Items, hours & payments")
        self._items(fig, s)
        self._hours(fig, s)
        self._payments(fig, s)
        self._discounts(fig, s)
        return fig

    def _items(self, fig: Figure, s: Section) -> None:
        it = s.items
        self._card(fig, 0.035, 0.07, 0.53, 0.78, "Top 15 items", "By net sales")
        df = pd.DataFrame(
            {
                "Item": [f"{i + 1:>2}  {n}" for i, n in enumerate(it["adjusted_item_variant"])],
                "Category": it["adjusted_category"],
                "Sold": it["items_sold"],
                "Net Sales": it["net_sales"].map(idr),
                "share": it["share"].fillna(0),
            }
        )
        row = 0.66 / 16
        ax = fig.add_axes((0.047, 0.75 - row * (len(df) + 1), 0.506, row * (len(df) + 1)))
        self._table(
            ax,
            df,
            [
                ColumnDefinition("Item", title="ITEM", width=1.9, textprops={"ha": "left"}),
                ColumnDefinition(
                    "Category",
                    title="CATEGORY",
                    width=1.3,
                    textprops={"ha": "left", "color": self.p.muted},
                ),
                ColumnDefinition("Sold", title="SOLD", width=0.5, textprops={"ha": "right"}),
                ColumnDefinition(
                    "Net Sales",
                    title="NET SALES",
                    width=0.9,
                    textprops={"ha": "right", "weight": "bold"},
                ),
                self._bar_column("share", "% OF NET", df["share"].max(), 1.1),
            ],
            "Item",
        )

    def _hours(self, fig: Figure, s: Section) -> None:
        self._card(
            fig,
            0.577,
            0.555,
            0.388,
            0.295,
            "Orders by hour",
            f"Average per trading day · {s.trading_days} days · top 3 hours highlighted",
        )
        hours = s.hours
        if hours.empty:
            return
        ax = fig.add_axes((0.595, 0.59, 0.355, 0.17))
        peak = hours["orders"].nlargest(3).min()
        ax.bar(
            hours["hour"],
            hours["orders"],
            width=0.7,
            color=[self.p.secondary if v >= peak else self.p.primary for v in hours["orders"]],
        )
        for hour, v in zip(hours["hour"], hours["orders"], strict=True):
            if v >= 0.05:
                ax.text(
                    hour, v, f"{v:.1f}", ha="center", va="bottom", fontsize=6.5, color=self.p.muted
                )
        ax.set_xticks(hours["hour"], [f"{h:02d}" for h in hours["hour"]])
        ax.set_yticks([])
        ax.spines["bottom"].set_color(self.p.grid)

    def _payments(self, fig: Figure, s: Section) -> None:
        self._card(fig, 0.577, 0.32, 0.388, 0.22, "Payment types", "Share of net sales")
        pay = s.payments[["payment_type", "orders", "net_sales", "share"]]
        if len(pay) > MAX_PAYMENT_TYPES:
            rest = pay.iloc[MAX_PAYMENT_TYPES - 1 :]
            other = {
                "payment_type": f"Other ({len(rest)})",
                "orders": rest["orders"].sum(),  # receipts have one payment type
                "net_sales": rest["net_sales"].sum(),
                "share": rest["share"].sum(),
            }
            pay = pd.concat([pay.iloc[: MAX_PAYMENT_TYPES - 1], pd.DataFrame([other])])
        ax = fig.add_axes((0.592, 0.445, 0.358, 0.035))
        left = 0.0
        for r, color in zip(pay.itertuples(), self.p.series, strict=False):
            share = 0 if pd.isna(r.share) else max(r.share, 0)
            ax.barh(0, share, left=left, color=color, height=1, edgecolor=self.p.card, lw=1.5)
            left += share
        ax.set_xlim(0, max(left, 1))
        ax.axis("off")
        for i, (r, color) in enumerate(zip(pay.itertuples(), self.p.series, strict=False)):
            lx, ly = 0.592 + (i % 2) * 0.18, 0.405 - (i // 2) * 0.05
            fig.text(lx, ly, "●", color=color, fontsize=9)
            fig.text(
                lx + 0.014,
                ly + 0.001,
                f"{r.payment_type}  {pct(r.share)}",
                fontsize=8.5,
                fontweight="semibold",
            )
            fig.text(
                lx + 0.014,
                ly - 0.021,
                f"{r.orders:,} orders · {compact(r.net_sales)} {project().currency.code}",
                fontsize=7.5,
                color=self.p.muted,
            )

    def _discounts(self, fig: Figure, s: Section) -> None:
        self._card(fig, 0.577, 0.07, 0.388, 0.235, "Discounts", "By discount code")
        d = s.discounts
        df = pd.DataFrame(
            {
                "Discount": d["code"],
                "Orders": d["orders"],
                "Gross": d["gross_sales"].map(idr),
                "Discounts": d["discounts"].map(idr),
                "Rate": d["rate"].map(pct),
            }
        )
        row = min(0.042, 0.158 / (len(df) + 1))
        ax = fig.add_axes((0.589, 0.235 - row * (len(df) + 1), 0.364, row * (len(df) + 1)))
        self._table(
            ax,
            df,
            [
                ColumnDefinition("Discount", title="CODE", width=1.2, textprops={"ha": "left"}),
                ColumnDefinition("Orders", title="ORDERS", width=0.6, textprops={"ha": "right"}),
                ColumnDefinition("Gross", title="GROSS", width=0.9, textprops={"ha": "right"}),
                ColumnDefinition(
                    "Discounts", title="DISCOUNTS", width=0.9, textprops={"ha": "right"}
                ),
                ColumnDefinition(
                    "Rate", title="RATE", width=0.6, textprops={"ha": "right", "weight": "bold"}
                ),
            ],
            "Discount",
            size=7.5,
        )


def write_pdf(
    path: Path,
    lines: pd.DataFrame,
    periods: list[Period],
    notes: list[str],
    sources: list[str],
    anchor: date | None = None,
    cover_png: Path | None = None,
) -> None:
    PdfReport(lines, notes, sources, anchor=anchor).build(path, periods, cover_png)
