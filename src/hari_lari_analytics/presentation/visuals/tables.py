"""The `table` visual: one row per group (or per metric), with bars, deltas, rank and the total.

It draws with plottable through the building blocks of today's report. Every string comes from
`present.py`; the bars use the raw values only for their length.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd
from plottable import ColumnDefinition

from hari_lari_analytics.analysis.result import Result
from hari_lari_analytics.presentation import formats
from hari_lari_analytics.presentation.present import Presentation, Row, present
from hari_lari_analytics.presentation.visuals import (
    Area,
    DrawContext,
    Options,
    VisualError,
    base,
    fits,
    need_compare,
    need_metrics,
    visual,
)
from hari_lari_analytics.presentation.visuals.common import change_color, n_label

ROW_POINTS = 24  # the most a row is high
MIN_ROW_POINTS = 11  # rows below this are left out, with a note


class TableOptions(Options):
    columns: list[str] | None = None  # the metrics to show, in order; default: all of the query
    bars: list[str] = []  # metrics drawn as bars, with the value written beside them
    deltas: dict[str, Literal["change", "delta"]] = {}  # a change column after the metric
    prior: list[str] = []  # a prior-value column after the metric
    bold: list[str] = []
    rank: bool = False  # number the rows
    total: bool = True  # show the total row, when the query has one
    fill: bool = False  # spread the rows over the whole area instead of stacking them at the top
    # groups: a row per group; metrics: a row per metric (current, prior, change), the
    # default for a result without dimensions
    orientation: Literal["auto", "groups", "metrics"] = "auto"


def _check(options: TableOptions, query, shape, registry) -> None:
    named = [*options.bars, *options.deltas, *options.prior, *options.bold]
    need_metrics("table", "columns", options.columns or [], query)
    for option in ("bars", "deltas", "prior", "bold"):
        need_metrics("table", option, list(getattr(options, option)), query)
    if options.columns is not None:
        missing = [m for m in named if m not in options.columns]
        if missing:
            raise VisualError(
                f"visual table: {', '.join(dict.fromkeys(missing))} is styled but not in columns"
            )
    if options.deltas:
        need_compare("table", "deltas", query)
    if options.prior:
        need_compare("table", "prior", query)
    if options.orientation == "metrics" and options.bars:
        raise VisualError("visual table: bars need a row per group, not orientation: metrics")


def _rows(result: Result, pres: Presentation, options: TableOptions) -> list[Row]:
    rows = []
    for row in pres.rows:
        if row.kind == "total" and (not options.total or row.label != "Total"):
            continue  # also the bucket totals of a series
        if "in_window" in result.df and not bool(result.df["in_window"].iloc[row.position]):
            continue
        rows.append(row)
    return rows


def _fit_rows(area: Area, rows: list[Row], header_rows: int) -> tuple[list[Row], int]:
    """The rows that fit the area at a readable height, and how many were left out."""
    fit = int(1 / area.points_y(MIN_ROW_POINTS)) - header_rows
    if len(rows) <= fit:
        return rows, 0
    keep = max(fit - 1, 1)
    total = [r for r in rows if r.kind == "total"]
    regular = [r for r in rows if r.kind != "total"]
    return [*regular[: keep - len(total)], *total], len(rows) - keep


def _draw(
    area: Area, df: pd.DataFrame, defs: list[ColumnDefinition], header_rows: int, fill: bool
):
    n = len(df) + header_rows
    row_h = 1 / n if fill else min(area.points_y(ROW_POINTS), 1 / n)
    ax = area.axes(0, 1 - row_h * n, 1, row_h * n)
    return base.table(ax, area.theme.palette, df, defs, "label", size=area.theme.size("body"))


def _style_total(table, idx: int, bar_columns: dict[int, str], area: Area) -> None:
    """Bold total row below a solid line; in a bar column its value is written, not drawn (as in
    today's report)."""
    for cell in table.rows[idx].cells:
        if hasattr(cell, "text"):
            cell.text.set_fontweight("bold")
    x0, x1 = table.rows[idx].get_xrange()
    table.ax.plot([x0, x1], [idx, idx], color=area.theme.color("muted"), lw=0.8)
    for col, text in bar_columns.items():
        sub = table.subplots[(idx, col)]
        sub.cla()
        sub.set_xlim(0, 1)
        sub.set_ylim(0, 1)
        sub.axis("off")
        sub.text(0, 0.5, text, va="center", fontsize=7.5, fontweight="bold")


def _metric_text(row: Row, metric: str, pres: Presentation, options: TableOptions) -> str:
    col = pres.columns[metric]
    text = f"{col.label} {row[metric].text}{' ' + col.unit if col.unit else ''}"
    if metric in options.deltas:
        change = row.cells.get(f"{metric}_{options.deltas[metric]}")
        if change is not None and change.text:
            text += f" ({change.text})"
    return text


def _table_text(result: Result, options: TableOptions, ctx: DrawContext) -> list[str]:
    """One line per row: its label, then each column as `label value`; a result without
    dimensions gives one line per metric with its change."""
    pres = present(result)
    metrics = options.columns or pres.metrics
    orientation = options.orientation
    if orientation == "auto":
        orientation = "metrics" if result.meta.shape == "single" else "groups"
    if orientation == "metrics":
        row = pres.total or pres.rows[0]
        compare = result.meta.compare_ok
        lines = []
        for m in metrics:
            spec = result.meta.metrics[m]
            line = _metric_text(row, m, pres, options.model_copy(update={"deltas": {}}))
            kind = options.deltas.get(m) or formats.default_change(spec)
            change = row.cells.get(f"{m}_{kind}")
            if compare and change is not None:
                line += f" ({change.text} vs {result.meta.window.compare_label})"
            lines.append(line)
        return lines
    rows = _rows(result, pres, options)
    if not rows:
        return ["No rows"]
    lines = []
    for row in rows:
        label = "Total" if row.kind == "total" else row.label
        if row.small:
            label += f" ({n_label(row.n)})"
        values = " · ".join(_metric_text(row, m, pres, options) for m in metrics)
        lines.append(f"{label}: {values}")
    return lines


@visual(
    "table",
    TableOptions,
    accepts=("rows", "single", "series"),
    min_size=(0.25, 0.15),
    check=_check,
    text=_table_text,
)
def table(area: Area, result: Result, options: TableOptions, ctx: DrawContext) -> None:
    """A table: one row per group with bars, deltas, rank and the total row, or one row per
    metric with its current and prior value and change."""
    orientation = options.orientation
    if orientation == "auto":
        orientation = "metrics" if result.meta.shape == "single" else "groups"
    if orientation == "metrics":
        _by_metric(area, result, options)
    else:
        _by_group(area, result, options)


def _by_group(area: Area, result: Result, options: TableOptions) -> None:
    theme = area.theme
    pres = present(result)
    metrics = options.columns or pres.metrics
    dims = [c for c, col in pres.columns.items() if col.kind == "dimension"]
    grouped = any(m in options.deltas or m in options.prior for m in metrics)
    rows, left_out = _fit_rows(area, _rows(result, pres, options), 1 + grouped)
    if not rows:
        area.text(0, 0.5, "No rows", color="muted", va="center")
        return

    data: dict[str, list] = {"label": []}
    defs = [
        ColumnDefinition(
            "label",
            title=(pres.columns[dims[0]].label if dims else "").upper(),
            width=1.9 if options.rank else 1.6,
            textprops={"ha": "left"},
        )
    ]
    for dim in dims[1:]:
        data[f"dim:{dim}"] = []
        defs.append(
            ColumnDefinition(
                f"dim:{dim}",
                title=pres.columns[dim].label.upper(),
                width=1.2,
                textprops={"ha": "left", "color": theme.color("muted")},
            )
        )
    bar_specs: dict[str, tuple[str, float]] = {}
    for m in metrics:
        col = pres.columns[m]
        label = col.label.upper()
        group = label if (m in options.deltas or m in options.prior) else None
        weight = "bold" if m in options.bold else "normal"
        if m in options.bars:
            data[f"bar:{m}"] = []
            regular = [r[m].value or 0 for r in rows if r.kind != "total"]
            vmax = max([abs(v) for v in regular] or [0])
            fmt = col.format

            def formatter(v, fmt=fmt):
                return formats.value_text(v, fmt)

            title = (col.unit or "NOW") if group else label
            defs.append(base.bar_column(theme.palette, f"bar:{m}", title, vmax, 1.15, formatter))
            bar_specs[m] = (f"bar:{m}", vmax)
        else:
            data[m] = []
            defs.append(
                ColumnDefinition(
                    m,
                    title=(col.unit or "NOW") if group else label,
                    group=group,
                    width=0.85,
                    textprops={"ha": "right", "weight": weight},
                )
            )
        if m in options.prior:
            data[f"prior:{m}"] = []
            defs.append(
                ColumnDefinition(
                    f"prior:{m}",
                    title="PRIOR",
                    group=group,
                    width=0.8,
                    textprops={"ha": "right", "color": theme.color("muted")},
                )
            )
        if m in options.deltas:
            data[f"delta:{m}"] = []
            defs.append(
                ColumnDefinition(
                    f"delta:{m}",
                    title="Δ",
                    group=group,
                    width=0.75,
                    textprops={"ha": "right", "weight": "semibold"},
                )
            )

    def first_label(row: Row, i: int) -> str:
        if row.kind == "total":
            return "Total"
        text = fits(row[dims[0]].text if dims else row.label, area, "body", 0.16)
        if row.small:
            text = f"{text} · {n_label(row.n)}"
        return f"{i + 1:>2}  {text}" if options.rank and row.kind == "row" else text

    for i, row in enumerate(rows):
        data["label"].append(first_label(row, i))
        for dim in dims[1:]:
            data[f"dim:{dim}"].append(row[dim].text)
        for m in metrics:
            if m in options.bars:
                data[f"bar:{m}"].append(float(row[m].value or 0))
            else:
                data[m].append(row[m].text)
            if m in options.prior:
                data[f"prior:{m}"].append(row[f"prior_{m}"].text)
            if m in options.deltas:
                data[f"delta:{m}"].append(row[f"{m}_{options.deltas[m]}"].text)
    df = pd.DataFrame(data)
    t = _draw(area, df, defs, 1 + grouped, options.fill)

    names = list(df.columns)
    for i, row in enumerate(rows):
        for m in metrics:
            if m in options.deltas:
                cell = row[f"{m}_{options.deltas[m]}"]
                t.cells[(i, names.index(f"delta:{m}"))].text.set_color(change_color(theme, cell))
        if row.small:
            for j, name in enumerate(names):
                if not name.startswith(("bar:", "delta:")):
                    t.cells[(i, j)].text.set_color(theme.color("muted"))
    for i, row in enumerate(rows):
        if row.kind == "total":
            bars = {names.index(c): row[m].text for m, (c, _) in bar_specs.items()}
            _style_total(t, i, bars, area)
    if left_out:
        area.text(0, 0, f"… and {left_out} more rows", role="small", color="muted")


def _by_metric(area: Area, result: Result, options: TableOptions) -> None:
    theme = area.theme
    pres = present(result)
    row = pres.total or pres.rows[0]
    metrics = options.columns or pres.metrics
    compare = result.meta.has_comparison
    data: dict[str, list] = {"label": [], "current": []}
    if compare:
        data |= {"prior": [], "change": []}
    changes = []
    for m in metrics:
        spec = result.meta.metrics[m]
        data["label"].append(spec.label)
        data["current"].append(row[m].text)
        if compare:
            data["prior"].append(row[f"prior_{m}"].text)
            kind = options.deltas.get(m) or formats.default_change(spec)
            cell = row.cells.get(f"{m}_{kind}")
            data["change"].append(cell.text if cell is not None else "")
            changes.append(cell)
    defs = [
        ColumnDefinition("label", title="METRIC", width=1.25, textprops={"ha": "left"}),
        ColumnDefinition(
            "current", title="CURRENT", width=1.1, textprops={"ha": "right", "weight": "bold"}
        ),
    ]
    if compare:
        muted = {"ha": "right", "color": theme.color("muted")}
        defs += [
            ColumnDefinition("prior", title="PRIOR", width=1.0, textprops=muted),
            ColumnDefinition(
                "change",
                title="CHANGE",
                width=0.85,
                textprops={"ha": "right", "weight": "semibold"},
            ),
        ]
    t = _draw(area, pd.DataFrame(data), defs, 1, options.fill)
    for i, cell in enumerate(changes):
        if cell is not None:
            t.cells[(i, 3)].text.set_color(change_color(theme, cell))
    for i, m in enumerate(metrics):
        if row[m].small:
            t.cells[(i, 1)].text.set_color(theme.color("muted"))
