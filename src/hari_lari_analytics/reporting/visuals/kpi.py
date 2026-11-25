"""KPI visuals: `kpi_card`, `summary_card` and `text_line`."""

from __future__ import annotations

from typing import Literal

import numpy as np

from hari_lari_analytics.reporting import formats
from hari_lari_analytics.reporting.compiler import Query
from hari_lari_analytics.reporting.present import Row, present
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.visuals import (
    Area,
    DrawContext,
    Options,
    VisualError,
    base,
    need_metrics,
    visual,
)
from hari_lari_analytics.reporting.visuals.common import (
    arrow,
    change_color,
    metric_of,
    n_label,
    series_rows,
    text_color,
    window_row,
)

Style = Literal["full", "compact", "headline"]


def _needs_totals(name: str, query, shape) -> None:
    if shape == "series" and isinstance(query, Query) and not query.totals:
        raise VisualError(
            f"visual {name}: on a series it shows the window total and a sparkline of the "
            "buckets, so the query needs `totals: true`"
        )


def _change_cell(row: Row, metric: str):
    for suffix in ("change", "delta"):
        if f"{metric}_{suffix}" in row.cells:
            return row[f"{metric}_{suffix}"]
    return None


def _spark_values(result: Result, metric: str) -> np.ndarray | None:
    if result.meta.shape != "series":
        return None
    pres = present(result)
    values = [r[metric].value for r in series_rows(result, pres)]
    values = np.array([0.0 if v is None else float(v) for v in values])
    return values if len(values) > 1 else None


def _comparison_cell(result: Result, row: Row, metric: str):
    """The change a card shows: the ratio, or percentage points for a share."""
    kind = formats.default_change(result.meta.metrics[metric])
    return row.cells.get(f"{metric}_{kind}") or _change_cell(row, metric)


# --- kpi_card -----------------------------------------------------------------------------------


class KpiCardOptions(Options):
    metric: str | None = None  # default: the facet's metric, else the first of the query
    prior: bool = True  # the change and the prior value, when the query compares
    sparkline: bool = True  # on a series: the buckets of the window
    style: Style = "headline"
    card: bool = True  # draw the card behind it


def _kpi_check(options: KpiCardOptions, query, shape, registry) -> None:
    need_metrics("kpi_card", "metric", [options.metric], query)
    _needs_totals("kpi_card", query, shape)


def _kpi_text(result: Result, options: KpiCardOptions, ctx: DrawContext) -> list[str]:
    metric = metric_of("kpi_card", result, options.metric, ctx.metric)
    spec = result.meta.metrics[metric]
    row = window_row(present(result, options.style))
    cell = row[metric]
    unit = formats.unit(spec.format)
    line = f"{spec.label}: {cell.text}{' ' + unit if unit else ''}"
    change = _comparison_cell(result, row, metric)
    window = result.meta.window
    if options.prior and change is not None and result.meta.compare_ok:
        line += f" ({change.text} vs {window.compare_label})"
    if cell.small:
        line += f" ({n_label(cell.n)})"
    return [line]


@visual(
    "kpi_card",
    KpiCardOptions,
    accepts=("single", "series"),
    min_size=(0.15, 0.1),
    check=_kpi_check,
    text=_kpi_text,
    labels_facets=True,
)
def kpi_card(area: Area, result: Result, options: KpiCardOptions, ctx: DrawContext) -> None:
    """One metric: its label, the value, the change with the prior value, and a sparkline."""
    theme = area.theme
    metric = metric_of("kpi_card", result, options.metric, ctx.metric)
    spec = result.meta.metrics[metric]
    row = window_row(present(result, options.style))
    cell = row[metric]
    if options.card:
        base.card(area.fig, theme.palette, *area.rect)
    px, py = area.points_x(10), area.points_y(10)
    body, muted = theme.size("body"), theme.color("muted")
    label = spec.label.upper()
    area.text(px, 1 - py, label, role="label", color="muted", weight="semibold", va="top")
    value = area.text(
        px, 0.36, cell.text, role="headline", weight="bold", color=text_color(theme, cell)
    )
    unit = formats.unit(spec.format)
    if unit and cell.value is not None:
        base.after(area.fig, value, unit, fontsize=body, color=muted)
    change = _comparison_cell(result, row, metric)
    line_y = 0.12
    if options.prior and result.meta.has_comparison:
        if result.meta.compare_ok and change is not None:
            last = area.text(
                px, line_y, arrow(change), weight="semibold", color=change_color(theme, change)
            )
            prior = row.cells.get(f"prior_{metric}")
            if prior is not None:
                text = f"prior {formats.value_text(prior.value, spec.format, 'compact')}"
                last = base.after(area.fig, last, text, gap=8, fontsize=body, color=muted)
            if cell.small:
                base.after(area.fig, last, n_label(cell.n), gap=8, fontsize=body, color=muted)
        else:
            area.text(px, line_y, "no prior period", color="muted")
    elif cell.small:
        area.text(px, line_y, n_label(cell.n), color="muted")
    values = _spark_values(result, metric) if options.sparkline else None
    if values is not None:
        x, y = area.at(0.58, 0.3)
        base.sparkline(area.fig, theme.palette, (x, y, area.w * 0.37, area.h * 0.38), values)


# --- summary_card -------------------------------------------------------------------------------


class SummaryCardOptions(Options):
    metrics: list[str] | None = None  # default: the first two of the query
    sparkline: str | None = None  # a metric; default: the first shown, on a series
    card: bool = True


def _summary_check(options: SummaryCardOptions, query, shape, registry) -> None:
    need_metrics("summary_card", "metrics", options.metrics or [], query)
    need_metrics("summary_card", "sparkline", [options.sparkline], query)
    _needs_totals("summary_card", query, shape)
    if options.metrics is not None and not 1 <= len(options.metrics) <= 3:
        raise VisualError("visual summary_card: metrics: one to three metrics fit on a card")


@visual(
    "summary_card",
    SummaryCardOptions,
    accepts=("single", "series"),
    min_size=(0.25, 0.25),
    check=_summary_check,
    labels_facets=True,
)
def summary_card(area: Area, result: Result, options: SummaryCardOptions, ctx: DrawContext) -> None:
    """A period card: the window, its comparison, up to three metrics with their change and a
    sparkline."""
    theme = area.theme
    window = result.meta.window
    metrics = options.metrics or list(result.meta.value_metrics)[:2]
    row = window_row(present(result, "headline"))
    if options.card:
        base.card(area.fig, theme.palette, *area.rect)
    px, top = area.points_x(11), 1 - area.points_y(11)
    title = ctx.label or (window.current.title if window else "")
    area.text(px, top, title, role="card_title", weight="bold", va="top")
    line = area.points_y(theme.size("card_title") * 1.6)
    if window is not None:
        area.text(px, top - line, window.current.label, role="body", color="muted", va="top")
        grain = formats.GRAIN_WORD[result.meta.grain or window.current.grain]
        compare = ""
        if window.compare != "none":
            compare = f"vs {window.compare_label}" if window.compare_ok else "no comparison"
        hint = " · ".join(t for t in (f"by {grain}", compare) if t)
        area.text(px, top - 2.1 * line, hint, role="hint", color="muted", va="top")
    spark = options.sparkline or (metrics[0] if result.meta.shape == "series" else None)
    bottom = 0.3 if spark else 0.08
    step = (top - 3.2 * line - bottom) / max(len(metrics), 1)
    body = theme.size("body")
    label_gap = area.points_y(theme.size("value") * 1.15)
    for i, metric in enumerate(metrics):
        spec = result.meta.metrics[metric]
        cell = row[metric]
        y = top - 3.2 * line - (i + 1) * step + step * 0.15
        label = spec.label.upper()
        area.text(px, y + label_gap, label, role="label", color="muted", weight="semibold")
        value = area.text(
            px, y, cell.text, role="value", weight="bold", color=text_color(theme, cell)
        )
        change = _comparison_cell(result, row, metric)
        if window is not None and window.compare_ok and change is not None:
            color = change_color(theme, change)
            value = base.after(
                area.fig, value, arrow(change), 6, fontsize=body, color=color, fontweight="semibold"
            )
        if cell.small:
            muted = theme.color("muted")
            base.after(area.fig, value, n_label(cell.n), 6, fontsize=body, color=muted)
    values = _spark_values(result, spark) if spark else None
    if values is not None:
        x, y = area.at(px, 0.06)
        rect = (x, y, area.w * (1 - 2 * px), area.h * 0.17)
        base.sparkline(area.fig, theme.palette, rect, values)


# --- text_line ----------------------------------------------------------------------------------


class TextLineOptions(Options):
    metrics: list[str] | None = None  # default: every metric of the query
    labels: dict[str, str] = {}  # a shorter label per metric
    style: Style = "full"


def _line_check(options: TextLineOptions, query, shape, registry) -> None:
    need_metrics("text_line", "metrics", options.metrics or [], query)
    need_metrics("text_line", "labels", list(options.labels), query)


def _line(result: Result, options: TextLineOptions) -> str:
    metrics = options.metrics or list(result.meta.value_metrics)
    row = window_row(present(result, options.style))
    units = {formats.unit(result.meta.metrics[m].format) for m in metrics}
    shared = units.pop() if len(units) == 1 else None
    parts = []
    for metric in metrics:
        spec = result.meta.metrics[metric]
        label = options.labels.get(metric, spec.label)
        unit = "" if shared is not None else formats.unit(spec.format)
        parts.append(f"{label} {row[metric].text}{' ' + unit if unit else ''}")
    return " · ".join(parts) + (f" {shared}" if shared else "")


def _line_text(result: Result, options: TextLineOptions, ctx: DrawContext) -> list[str]:
    return [_line(result, options)]


@visual(
    "text_line",
    TextLineOptions,
    accepts=("single",),
    min_size=(0.3, 0.03),
    check=_line_check,
    text=_line_text,
)
def text_line(area: Area, result: Result, options: TextLineOptions, ctx: DrawContext) -> None:
    """One line of values: Gross 1,234,000 · discounts 12,000 · refunds 0 IDR."""
    area.text(0, 1, _line(result, options), role="body", color="muted", va="top")
