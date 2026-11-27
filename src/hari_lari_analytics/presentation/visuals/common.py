"""Marks shared by the visuals: arrows, colours of cells, the `n=` label, the rows of a series,
the axis labels of buckets and the calendar-event marks."""

from __future__ import annotations

from datetime import date

import pandas as pd

from hari_lari_analytics.analysis.result import Result
from hari_lari_analytics.analysis.windows import DayEvent
from hari_lari_analytics.presentation import formats
from hari_lari_analytics.presentation.formats import NA
from hari_lari_analytics.presentation.present import Cell, Presentation, Row
from hari_lari_analytics.presentation.visuals import Area, VisualError, base
from hari_lari_analytics.presentation.visuals.theme import EVENT_KINDS, TOKENS, Theme, Token

PALE = 0.4  # the opacity of a bar or dot below the sample-size rule


def arrow(cell: Cell) -> str:
    """A change with its arrow: ▲ +12.3%, ▼ -4.0%; n/a and – as they are."""
    if cell.value is None or cell.text == NA:
        return cell.text
    return ("▲ " if cell.value >= 0 else "▼ ") + cell.text


def text_color(theme: Theme, cell: Cell, token: Token = "text") -> str:
    """A value's colour: muted when small or missing."""
    return theme.color("muted" if cell.small or cell.value is None else token)


def change_color(theme: Theme, cell: Cell) -> str:
    return theme.tone_color(cell.tone, cell.small)


def n_label(n: int | None) -> str:
    return "" if n is None else f"n={n}"


def metric_of(name: str, result: Result, chosen: str | None, ctx_metric: str | None) -> str:
    """The metric a visual shows: the option, else the facet's, else the first of the result."""
    metric = chosen or ctx_metric or next(iter(result.meta.value_metrics), None)
    if metric is None or metric not in result.df:
        have = ", ".join(result.meta.value_metrics) or "none"
        raise VisualError(f"visual {name}: the result has no metric {metric} ({have})")
    return metric


def window_row(pres: Presentation) -> Row:
    """The row of the whole window: the total, or the only row."""
    if pres.total is not None:
        return pres.total
    if len(pres.rows) == 1:
        return pres.rows[0]
    raise VisualError("the result has no total row: set `totals: true` on the query")


def series_rows(result: Result, pres: Presentation) -> list[Row]:
    """The rows to draw along an axis: the regular rows inside the window (without the bucket
    totals and the buckets before the window)."""
    df = result.df
    rows = []
    for row in pres.rows:
        if row.kind == "total":
            continue
        if "in_window" in df and not bool(df["in_window"].iloc[row.position]):
            continue
        rows.append(row)
    return rows


def time_column(result: Result) -> str | None:
    for dim_id, dim in result.meta.dimensions.items():
        if dim.domain == "calendar" and dim_id in result.df:
            return dim_id
    return None


def bucket_ticks(result: Result, rows: list[Row]) -> list[str]:
    """The axis labels: two-line bucket labels on a series, the value labels otherwise."""
    column = time_column(result)
    if column is None or result.meta.grain is None:
        dims = [d for d in result.meta.dimensions if d in result.df]
        return [r[dims[0]].text if dims else r.label for r in rows]
    days = [pd.Timestamp(result.df[column].iloc[r.position]).date() for r in rows]
    return [formats.bucket_tick(d, result.meta.grain) for d in days]


def row_dates(result: Result, rows: list[Row]) -> list[tuple[date, date] | None]:
    """The days each row covers: its bucket on a series, its day for a day dimension."""
    column = time_column(result)
    if column is None:
        return [None] * len(rows)
    ends = {b.start: b.end for b in result.meta.buckets}
    out: list[tuple[date, date] | None] = []
    for r in rows:
        value = result.df[column].iloc[r.position]
        if pd.isna(value):
            out.append(None)
            continue
        start = pd.Timestamp(value).date()
        out.append((start, ends.get(start, start)))
    return out


def events_in(result: Result, span: tuple[date, date] | None) -> list[DayEvent]:
    if span is None:
        return []
    return [e for e in result.meta.events if span[0] <= e.date <= span[1]]


def mark_events(ax, area: Area, result: Result, rows: list[Row], xs: list[float]) -> list[str]:
    """A small triangle under each bucket that holds a holiday, closure or promotion (the most
    telling kind of its days); returns the kinds drawn, for the legend."""
    theme = area.theme
    drawn: list[str] = []
    order = {"closure": 0, "holiday": 1, "promotion": 2, "other": 3}
    for x, span in zip(xs, row_dates(result, rows), strict=True):
        kinds = sorted({e.kind for e in events_in(result, span)}, key=lambda k: order.get(k, 9))
        kinds = [k for k in kinds if k in EVENT_KINDS]
        if not kinds:
            continue
        ax.plot(
            [x],
            [0.03],
            marker="^",
            markersize=5,
            color=theme.color(kinds[0]),
            transform=ax.get_xaxis_transform(),
            clip_on=False,
            zorder=5,
        )
        drawn += [k for k in kinds if k not in drawn]
    return drawn


def legend(area: Area, entries: list[tuple[str, str, str]], fx: float = 0.0, fy: float = 0.0):
    """A one-line legend, left-aligned from (fx, fy): each entry is (mark, colour token or
    colour, text), as `▲ holiday   — prior period`."""
    theme = area.theme
    size = theme.size("small")
    anchor = None
    for mark, color, text in entries:
        resolved = theme.color(color) if color in TOKENS else color
        if anchor is None:
            anchor = area.text(fx, fy, mark, role="small", color=resolved, va="bottom")
        else:
            anchor = base.after(area.fig, anchor, mark, 8, fontsize=size, color=resolved)
        anchor = base.after(area.fig, anchor, text, 2, fontsize=size, color=theme.color("muted"))


def event_entries(kinds: list[str]) -> list[tuple[str, str, str]]:
    return [("▲", kind, kind) for kind in kinds]


def numbers(rows: list[Row], column: str) -> list[float]:
    """The values of a column for drawing: a missing value is drawn as 0."""
    return [float(r[column].value) if r[column].value is not None else 0.0 for r in rows]
