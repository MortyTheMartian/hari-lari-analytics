"""PDF report: a cover page plus three pages per period (L14D, L6W, L3M), drawn with matplotlib.

All colours come from a `Palette`; pass another one to `PdfReport` to re-theme the report.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch
from matplotlib.text import Annotation, Text
from matplotlib.ticker import FuncFormatter
from plottable import ColumnDefinition, Table
from plottable.plots import bar

from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import Period
from hari_lari_analytics.report_data import GRAIN_NAME, GROWTH_NAME, Section, build_section

FONTS = Path(__file__).parent / "fonts"
for _font in FONTS.glob("*.ttf"):
    font_manager.fontManager.addfont(str(_font))

W, H = 11.69, 8.27  # A4 landscape, inches
MAX_PAYMENT_TYPES = 4


@dataclass(frozen=True)
class Palette:
    bg: str
    card: str
    edge: str
    text: str
    muted: str
    primary: str  # bars and increases
    secondary: str  # prior period, highlights
    negative: str  # decreases
    grid: str
    series: tuple[str, ...]  # payment types


DAYLIGHT = Palette(
    bg="#f3f5f8",
    card="#ffffff",
    edge="#e3e7ed",
    text="#0f172a",
    muted="#64748b",
    primary="#0e7c7b",
    secondary="#ff6b4a",
    negative="#d9480f",
    grid="#e9edf2",
    series=("#0e7c7b", "#ff6b4a", "#f2b84b", "#7c8db5"),
)


def idr(value: float) -> str:
    return "–" if pd.isna(value) else f"{value:,.0f}"


def pct(value: float, digits: int = 1) -> str:
    return "–" if pd.isna(value) else f"{value:.{digits}%}"


def compact(value: float) -> str:
    if pd.isna(value):
        return "–"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 10_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:,.0f}"


def headline(value: float, is_idr: bool) -> str:
    """Big KPI number: millions for large amounts, otherwise the full figure."""
    if pd.isna(value):
        return "–"
    return f"{value / 1e6:.2f}M" if is_idr and value >= 1e6 else f"{value:,.0f}"


def stamp(value) -> str:
    return "–" if pd.isna(value) else f"{value:%d %b %H:%M}"


def change_text(value: float, digits: int = 1) -> str:
    if pd.isna(value):
        return "n/a"
    text = ">+999%" if value > 9.99 else f"{value:+.{digits}%}"
    return ("▲ " if value >= 0 else "▼ ") + text


class PdfReport:
    def __init__(
        self,
        lines: pd.DataFrame,
        notes: list[str],
        sources: list[str],
        palette: Palette = DAYLIGHT,
    ):
        self.lines = lines
        self.notes = notes
        self.sources = sources
        self.p = palette
        self.first_date: date = lines["sale_ts"].min().date()
        self.anchor: date = lines["sale_ts"].max().date()
        self.generated = datetime.now()

    def build(self, path: Path, periods: list[Period]) -> None:
        sections = [build_section(self.lines, period, self.first_date) for period in periods]
        with matplotlib.rc_context(self._rc()), PdfPages(path) as pdf:
            pdf.savefig(self._cover(sections))
            full = [self._overview, self._categories, self._details]
            for s in sections:
                for page in [self._empty] if s.empty else full:
                    pdf.savefig(page(s))
            info = pdf.infodict()
            info["Title"] = f"Hari Lari sales report through {self.anchor:%d %b %Y}"

    # Building blocks

    def _rc(self) -> dict:
        p = self.p
        return {
            "font.family": "Inter",
            "text.color": p.text,
            "axes.labelcolor": p.muted,
            "xtick.color": p.muted,
            "ytick.color": p.muted,
            "axes.facecolor": "none",
            "axes.edgecolor": p.grid,
            "figure.facecolor": p.bg,
            "savefig.facecolor": p.bg,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "pdf.fonttype": 42,
        }

    def _color(self, value: float, up_is_good: bool = True) -> str:
        if pd.isna(value):
            return self.p.muted
        return self.p.primary if (value >= 0) == up_is_good else self.p.negative

    def _card(self, fig: Figure, x, y, w, h, title: str = "", hint: str = "") -> None:
        fig.patches.append(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0,rounding_size=0.008",
                mutation_aspect=W / H,
                transform=fig.transFigure,
                facecolor=self.p.card,
                edgecolor=self.p.edge,
                linewidth=0.8,
                zorder=-10,
            )
        )
        if title:
            fig.text(x + 0.014, y + h - 0.032, title, fontsize=10, fontweight="semibold")
        if hint:
            fig.text(x + 0.014, y + h - 0.055, hint, fontsize=7.5, color=self.p.muted)

    def _chips(self, fig: Figure, chips: list[tuple[str, str]]) -> None:
        """Pill-shaped labels, right-aligned in the header; style is "fill", "line" or "muted"."""
        size, pad = 8, 0.75
        anchor = None
        for text, style in reversed(chips):
            color = {"fill": self.p.card, "line": self.p.text, "muted": self.p.muted}[style]
            kw = {
                "fontsize": size,
                "color": color,
                "fontweight": "semibold" if style == "fill" else "medium",
                "ha": "right",
                "va": "center",
                "bbox": {
                    "boxstyle": f"round,pad={pad},rounding_size={pad + 0.5}",
                    "facecolor": self.p.primary if style == "fill" else self.p.card,
                    "edgecolor": self.p.primary if style == "fill" else self.p.edge,
                    "linewidth": 0.8,
                },
            }
            if anchor is None:
                anchor = fig.text(0.965, 0.912, text, **kw)
            else:
                anchor = Annotation(
                    text,
                    xy=(0, 0.5),
                    xycoords=anchor,
                    xytext=(-(2 * pad * size + 8), 0),
                    textcoords="offset points",
                    **kw,
                )
                fig.add_artist(anchor)

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
        """Place `text` right after `anchor`, using its rendered extent."""
        fig.add_artist(
            Annotation(
                text,
                xy=(1, 0),
                xycoords=anchor,
                xytext=(gap, 0),
                textcoords="offset points",
                va="bottom",
                **kw,
            )
        )

    def _sparkline(self, fig: Figure, rect, values: np.ndarray) -> None:
        ax = fig.add_axes(rect)
        ax.fill_between(
            range(len(values)), values, values.min() * 0.9, color=self.p.primary, alpha=0.14, lw=0
        )
        ax.plot(values, color=self.p.primary, lw=1.2 if len(values) > 31 else 1.4)
        ax.scatter([len(values) - 1], [values[-1]], s=12, color=self.p.secondary, zorder=3)
        ax.axis("off")

    def _table(self, ax, df: pd.DataFrame, defs: list[ColumnDefinition], index: str, size=8.0):
        ax.set_axis_off()
        table = Table(
            df,
            ax=ax,
            index_col=index,
            column_definitions=defs,
            textprops={"fontsize": size, "color": self.p.text, "fontfamily": "Inter"},
            row_dividers=True,
            row_divider_kw={"linewidth": 0.5, "color": self.p.grid},
            col_label_divider_kw={"linewidth": 0.8, "color": self.p.muted},
            col_label_cell_kw={"facecolor": self.p.card},
            cell_kw={"facecolor": self.p.card, "edgecolor": "none"},
        )
        for cell in table.col_label_row.cells:
            cell.text.set_color(self.p.muted)
            cell.text.set_fontsize(6.8)
            cell.text.set_fontweight("semibold")
        return table

    def _bar_column(self, name: str, title: str, vmax: float, width: float) -> ColumnDefinition:
        return ColumnDefinition(
            name,
            title=title,
            width=width,
            plot_fn=bar,
            plot_kw={
                "xlim": (0, (vmax if vmax > 0 else 1) * 2.1),
                "color": self.p.primary,
                "annotate": True,
                "height": 0.42,
                "lw": 0,
                "formatter": pct,
                "textprops": {"fontsize": 7.5, "color": self.p.text},
            },
        )

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
        x0, w, gap, y, h = 0.035, 0.2225, 0.0133, 0.70, 0.13
        for i, (label, value, unit) in enumerate(
            [
                ("Net sales · full history", headline(t.net_sales, True), "IDR"),
                ("Orders", headline(t.orders, False), ""),
                ("Avg order value", headline(t.aov, True), "IDR"),
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
            f"refunds {idr(t.refunds)} IDR",
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
        hint = "IDR · " + (
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
        self._card(fig, x, y, w, h, "Key figures", "Amounts in IDR · current vs prior period")
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

    def _categories(self, s: Section) -> Figure:
        fig = self._section_page(s, "Categories")
        c = s.categories
        hint = "Sorted by net sales" + (
            " · Δ in percentage points vs prior period" if s.prior_ok else ""
        )
        self._card(fig, 0.035, 0.07, 0.56, 0.78, "Adjusted categories", hint)
        df = pd.DataFrame(
            {
                "Category": c["adjusted_category"],
                "Net Sales": c["net_sales"].map(idr),
                "share": c["share"].fillna(0),
                "Items": c["items_sold"],
                "Orders": c["orders"],
                "Penetration": c["penetration"].map(pct),
                "delta": c["delta_pp"] if s.prior_ok else np.nan,
            }
        )
        row = min(0.06, 0.64 / (len(df) + 1))
        ax = fig.add_axes((0.047, 0.76 - row * (len(df) + 1), 0.536, row * (len(df) + 1)))
        self._table(
            ax,
            df,
            [
                ColumnDefinition("Category", title="CATEGORY", width=1.5, textprops={"ha": "left"}),
                ColumnDefinition(
                    "Net Sales",
                    title="NET SALES",
                    width=1.0,
                    textprops={"ha": "right", "weight": "bold"},
                ),
                self._bar_column("share", "% OF NET", df["share"].max(), 1.4),
                ColumnDefinition("Items", title="ITEMS", width=0.55, textprops={"ha": "right"}),
                ColumnDefinition("Orders", title="ORDERS", width=0.6, textprops={"ha": "right"}),
                ColumnDefinition(
                    "Penetration", title="PENETR.", width=0.75, textprops={"ha": "right"}
                ),
                ColumnDefinition(
                    "delta",
                    title="Δ PP",
                    width=0.75,
                    textprops={"ha": "right", "weight": "semibold"},
                    formatter=lambda v: "n/a" if pd.isna(v) else f"{v:+.1f}",
                    text_cmap=self._color,
                ),
            ],
            "Category",
            size=8.5,
        )

        hint = "Share of orders containing the category" + (
            " · ○ prior period" if s.prior_ok else ""
        )
        self._card(fig, 0.607, 0.07, 0.358, 0.78, "Order penetration", hint)
        p = c.sort_values("penetration").fillna({"penetration": 0, "prior_penetration": 0})
        ax = fig.add_axes((0.71, 0.12, 0.23, 0.63))
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
        return fig

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
                f"{r.orders:,} orders · {compact(r.net_sales)} IDR",
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
    path: Path, lines: pd.DataFrame, periods: list[Period], notes: list[str], sources: list[str]
) -> None:
    PdfReport(lines, notes, sources).build(path, periods)
