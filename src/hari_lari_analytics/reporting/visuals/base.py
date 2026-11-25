"""The drawing building blocks of today's report: card, chips, text after text, sparkline, the
plottable table and its bar column, and the colour of a change.

They were methods of `PdfReport` and moved here unchanged (plan 8.1), as functions that take
the palette; the old report calls them through thin wrappers, and the visuals use them too.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch
from matplotlib.text import Annotation, Text
from plottable import ColumnDefinition, Table
from plottable.plots import bar

from hari_lari_analytics.reporting.formats import pct
from hari_lari_analytics.reporting.visuals.theme import H, Palette, W


def change_color(p: Palette, value: float, up_is_good: bool = True) -> str:
    if pd.isna(value):
        return p.muted
    return p.primary if (value >= 0) == up_is_good else p.negative


def card(fig: Figure, p: Palette, x, y, w, h, title: str = "", hint: str = "") -> None:
    fig.patches.append(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0,rounding_size=0.008",
            mutation_aspect=W / H,
            transform=fig.transFigure,
            facecolor=p.card,
            edgecolor=p.edge,
            linewidth=0.8,
            zorder=-10,
        )
    )
    if title:
        fig.text(x + 0.014, y + h - 0.032, title, fontsize=10, fontweight="semibold")
    if hint:
        fig.text(x + 0.014, y + h - 0.055, hint, fontsize=7.5, color=p.muted)


def chips(fig: Figure, p: Palette, items: list[tuple[str, str]]) -> None:
    """Pill-shaped labels, right-aligned in the header; style is "fill", "line" or "muted"."""
    size, pad = 8, 0.75
    anchor = None
    for text, style in reversed(items):
        color = {"fill": p.card, "line": p.text, "muted": p.muted}[style]
        kw = {
            "fontsize": size,
            "color": color,
            "fontweight": "semibold" if style == "fill" else "medium",
            "ha": "right",
            "va": "center",
            "bbox": {
                "boxstyle": f"round,pad={pad},rounding_size={pad + 0.5}",
                "facecolor": p.primary if style == "fill" else p.card,
                "edgecolor": p.primary if style == "fill" else p.edge,
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


def after(fig: Figure, anchor: Text, text: str, gap: float = 4, **kw) -> Annotation:
    """Place `text` right after `anchor`, using its rendered extent."""
    artist = Annotation(
        text,
        xy=(1, 0),
        xycoords=anchor,
        xytext=(gap, 0),
        textcoords="offset points",
        va="bottom",
        **kw,
    )
    fig.add_artist(artist)
    return artist


def sparkline(fig: Figure, p: Palette, rect, values: np.ndarray) -> None:
    ax = fig.add_axes(rect)
    ax.fill_between(
        range(len(values)), values, values.min() * 0.9, color=p.primary, alpha=0.14, lw=0
    )
    ax.plot(values, color=p.primary, lw=1.2 if len(values) > 31 else 1.4)
    ax.scatter([len(values) - 1], [values[-1]], s=12, color=p.secondary, zorder=3)
    ax.axis("off")


def table(
    ax, p: Palette, df: pd.DataFrame, defs: list[ColumnDefinition], index: str, size=8.0
) -> Table:
    ax.set_axis_off()
    t = Table(
        df,
        ax=ax,
        index_col=index,
        column_definitions=defs,
        textprops={"fontsize": size, "color": p.text, "fontfamily": "Inter"},
        row_dividers=True,
        row_divider_kw={"linewidth": 0.5, "color": p.grid},
        col_label_divider_kw={"linewidth": 0.8, "color": p.muted},
        col_label_cell_kw={"facecolor": p.card},
        cell_kw={"facecolor": p.card, "edgecolor": "none"},
    )
    for cell in t.col_label_row.cells:
        cell.text.set_color(p.muted)
        cell.text.set_fontsize(6.8)
        cell.text.set_fontweight("semibold")
    for cell in getattr(t, "col_group_cells", {}).values():
        cell.text.set_color(p.text)
        cell.text.set_fontsize(7)
        cell.text.set_fontweight("semibold")
    return t


def bar_column(
    p: Palette, name: str, title: str, vmax: float, width: float, formatter=pct
) -> ColumnDefinition:
    return ColumnDefinition(
        name,
        title=title,
        width=width,
        plot_fn=bar,
        plot_kw={
            "xlim": (0, (vmax if vmax > 0 else 1) * 2.1),
            "color": p.primary,
            "annotate": True,
            "height": 0.42,
            "lw": 0,
            "formatter": formatter,
            "textprops": {"fontsize": 7.5, "color": p.text},
        },
    )
