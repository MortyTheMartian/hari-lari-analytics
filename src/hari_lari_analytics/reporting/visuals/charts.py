"""The charts: bar_trend, hbar, dot_plot, stacked_share_bar, heatmap, scatter_quadrant and
waterfall. Their labels come from `present.py`; the raw values only place the marks."""

from __future__ import annotations

import numpy as np
from matplotlib.colors import to_rgba
from matplotlib.ticker import FuncFormatter
from pydantic import Field

from hari_lari_analytics.reporting import formats
from hari_lari_analytics.reporting.present import Presentation, Row, present
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.visuals import (
    Area,
    DrawContext,
    Options,
    VisualError,
    base,
    fits,
    need_metrics,
    query_metrics,
    visual,
)
from hari_lari_analytics.reporting.visuals.common import (
    PALE,
    bucket_ticks,
    change_color,
    event_entries,
    legend,
    mark_events,
    metric_of,
    n_label,
    numbers,
    series_rows,
    text_color,
)
from hari_lari_analytics.reporting.visuals.theme import Token

LEGEND_POINTS = 12  # the legend line at the bottom of a chart


class Highlight(Options):
    top: int = Field(ge=1)  # the n largest values in the highlight colour


def _style(ax, area: Area, grid: str | None = "y") -> None:
    theme = area.theme
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(theme.color("grid"))
    ax.set_facecolor("none")
    ax.tick_params(labelsize=theme.size("label"), colors=theme.color("muted"), length=0, pad=3)
    if grid:
        ax.grid(axis=grid, color=theme.color("grid"), lw=0.6)
        ax.set_axisbelow(True)


def _value_formatter(result: Result, metric: str) -> FuncFormatter:
    fmt = result.meta.metrics[metric].format
    return FuncFormatter(lambda v, _: formats.value_text(v, fmt, "compact"))


def _rows(result: Result, pres: Presentation) -> list[Row]:
    if result.meta.shape == "series":
        return series_rows(result, pres)
    return [r for r in pres.rows if r.kind != "total"]


def _highlighted(values: list[float], highlight: Highlight | None) -> set[int]:
    if highlight is None:
        return set()
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    return set(order[: highlight.top])


def _small_entry(rows: list[Row]) -> list[tuple[str, str, str]]:
    return [("■", "muted", "pale: small sample")] if any(r.small for r in rows) else []


# --- bar_trend ------------------------------------------------------------------------------


class BarTrendOptions(Options):
    metric: str | None = None  # default: the facet's metric, else the first of the result
    line: str | None = None  # a second metric as a line below the bars (orders under sales)
    prior: bool = True  # the prior period as a tick on each bar, when the comparison is valid
    growth: bool = True  # the change against the previous bucket, when the result has it
    labels: bool = True  # the value above each bar
    highlight: Highlight | None = None
    events: bool = True  # mark the buckets with a holiday, closure or promotion
    color: Token = "accent"


def _bar_trend_check(options: BarTrendOptions, query, shape, registry) -> None:
    need_metrics("bar_trend", "metric", [options.metric, options.line], query)


@visual(
    "bar_trend",
    BarTrendOptions,
    accepts=("series", "rows"),
    min_size=(0.3, 0.25),
    check=_bar_trend_check,
)
def bar_trend(area: Area, result: Result, options: BarTrendOptions, ctx: DrawContext) -> None:
    """Bars of one metric along time (or along a dimension), with the prior period as ticks, the
    change against the previous bucket, an optional second metric as a line and the calendar
    events marked."""
    theme = area.theme
    metric = metric_of("bar_trend", result, options.metric, ctx.metric)
    pres, short = present(result), present(result, "compact")
    rows = _rows(result, pres)
    texts = {r.position: r for r in short.rows}
    if not rows:
        return
    values = numbers(rows, metric)
    idx = np.arange(len(rows))
    width = 0.62 if len(rows) > 6 else 0.5
    growth = options.growth and f"{metric}_growth" in pres.columns
    prior = options.prior and pres.compare_ok and f"prior_{metric}" in pres.columns

    legend_h = area.points_y(LEGEND_POINTS)
    tick_h = area.points_y(theme.size("label") * 3.2)
    strip_h = 0.16 if options.line else 0
    left = area.points_x(theme.size("label") * 4)
    bottom = legend_h + strip_h + tick_h
    ax = area.axes(left, bottom, 1 - left, 1 - bottom)
    _style(ax, area)
    top = set() if options.highlight is None else _highlighted(values, options.highlight)
    colors = [theme.color("highlight" if i in top else options.color) for i in range(len(rows))]
    alphas = [PALE if r.small else 1.0 for r in rows]
    for i, (v, color, alpha) in enumerate(zip(values, colors, alphas, strict=True)):
        ax.bar(i, v, width=width, color=color, alpha=alpha, edgecolor="none")
    high = max(values + [0])
    tick = "text" if top else "prior"  # the highlight has the prior's colour
    above = list(values)  # the labels sit above the bar or its prior tick, whichever is higher
    if prior:
        prior_values = numbers(rows, f"prior_{metric}")
        ax.hlines(
            prior_values, idx - width * 0.62, idx + width * 0.62, color=theme.color(tick), lw=1.8
        )
        high = max(high, *prior_values)
        above = [max(v, p) for v, p in zip(values, prior_values, strict=True)]
    for i, row in enumerate(rows):
        label = texts[row.position][metric]
        if options.labels:
            ax.annotate(
                label.text,
                (i, above[i]),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=theme.size("label"),
                fontweight="medium",
                color=text_color(theme, label),
            )
        if growth:
            cell = texts[row.position][f"{metric}_growth"]
            if cell.value is not None:
                ax.annotate(
                    cell.text,
                    (i, above[i]),
                    xytext=(0, 13 if options.labels else 3),
                    textcoords="offset points",
                    ha="center",
                    fontsize=theme.size("small"),
                    fontweight="semibold",
                    color=change_color(theme, cell),
                )
    ax.yaxis.set_major_formatter(_value_formatter(result, metric))
    ax.set_xticks(idx, bucket_ticks(result, rows))
    ax.set_xlim(-0.6, len(rows) - 0.4)
    ax.set_ylim(min(0, min(values)), max(high, 1) * (1.2 if growth else 1.12))
    kinds = mark_events(ax, area, result, rows, list(idx)) if options.events else []

    if options.line:
        _line_strip(area, result, rows, texts, options.line, idx, ax, (left, legend_h, strip_h))
    entries = []
    if prior:
        entries.append(("—", tick, "prior period"))
    entries += _small_entry(rows) + event_entries(kinds)
    if entries:
        legend(area, entries, left, 0)


def _line_strip(area, result, rows, texts, line: str, idx, bars_ax, rect) -> None:
    theme = area.theme
    left, y, h = rect
    ax = area.axes(left, y + area.points_y(4), 1 - left, h - area.points_y(4), sharex=bars_ax)
    ax.axis("off")
    values = numbers(rows, line)
    pres_cols = texts[rows[0].position].cells
    if f"prior_{line}" in pres_cols and result.meta.compare_ok:
        ax.plot(
            idx, numbers(rows, f"prior_{line}"), color=theme.color("prior"), lw=1, ls=(0, (3, 2))
        )
    ax.plot(
        idx,
        values,
        color=theme.color("text"),
        lw=1.3,
        marker="o",
        ms=3.5,
        mfc=theme.color("card"),
        mew=1.2,
    )
    for i, row in enumerate(rows):
        cell = texts[row.position][line]
        ax.annotate(
            cell.text,
            (i, values[i]),
            xytext=(0, 5),
            textcoords="offset points",
            ha="center",
            fontsize=theme.size("small"),
            color=theme.color("muted"),
        )
    ax.set_ylim(0, max(max(values + [0]), 1) * 1.5)
    label = result.meta.metrics[line].label
    area.text(0, y + h / 2, label, role="label", color="muted", va="center")


# --- hbar and dot_plot -----------------------------------------------------------------------


class HbarOptions(Options):
    metric: str | None = None
    prior: bool = True  # the prior value as a tick, when the comparison is valid
    highlight: Highlight | None = None
    labels: bool = True


def _metric_check(name: str):
    def check(options, query, shape, registry) -> None:
        need_metrics(name, "metric", [options.metric], query)

    return check


def _category_axes(area: Area, rows: list[Row], ticks: bool, label_width: float = 0.3):
    """Axes for one row per category from top to bottom, with the labels at the left; the
    value axis only when the marks are not labelled (`ticks`)."""
    theme = area.theme
    bottom = area.points_y(LEGEND_POINTS + (theme.size("label") * 2 if ticks else 0))
    ax = area.axes(label_width, bottom, 1 - label_width - 0.02, 1 - bottom)
    _style(ax, area, grid="x" if ticks else None)
    ax.spines["bottom"].set_visible(False)
    if not ticks:
        ax.set_xticks([])
    yy = np.arange(len(rows))[::-1]
    labels = []
    for r in rows:
        text = fits(r.label, area, "label", label_width * (0.7 if r.small else 0.95))
        labels.append(f"{text} · {n_label(r.n)}" if r.small else text)
    ax.set_yticks(yy, labels, fontsize=theme.size("label"))
    for tick, r in zip(ax.get_yticklabels(), rows, strict=True):
        tick.set_color(theme.color("muted" if r.small else "text"))
    ax.set_ylim(-0.6, len(rows) - 0.4)
    return ax, yy


@visual("hbar", HbarOptions, accepts=("rows",), min_size=(0.25, 0.2), check=_metric_check("hbar"))
def hbar(area: Area, result: Result, options: HbarOptions, ctx: DrawContext) -> None:
    """Horizontal bars of one metric, one per row in the result's order, with the prior value
    as a tick."""
    theme = area.theme
    metric = metric_of("hbar", result, options.metric, ctx.metric)
    pres = present(result)
    rows = _rows(result, pres)
    if not rows:
        return
    values = numbers(rows, metric)
    ax, yy = _category_axes(area, rows, ticks=not options.labels)
    top = _highlighted(values, options.highlight)
    for i, (y, v, r) in enumerate(zip(yy, values, rows, strict=True)):
        color = theme.color("highlight" if i in top else "accent")
        ax.barh(y, v, height=0.62, color=color, alpha=PALE if r.small else 1.0)
    high = max(values + [0])
    prior = options.prior and pres.compare_ok and f"prior_{metric}" in pres.columns
    if prior:
        prior_values = numbers(rows, f"prior_{metric}")
        ax.vlines(prior_values, yy - 0.38, yy + 0.38, color=theme.color("prior"), lw=1.8)
        high = max(high, *prior_values)
    ax.set_xlim(0, max(high, 1e-9) * 1.2)
    ax.xaxis.set_major_formatter(_value_formatter(result, metric))
    if options.labels:
        for y, v, r in zip(yy, values, rows, strict=True):
            ax.annotate(
                r[metric].text,
                (v, y),
                xytext=(3, 0),
                textcoords="offset points",
                va="center",
                fontsize=theme.size("label"),
                color=text_color(theme, r[metric]),
            )
    entries = ([("|", "prior", "prior period")] if prior else []) + _small_entry(rows)
    if entries:
        legend(area, entries, 0.3, 0)


class DotPlotOptions(Options):
    metric: str | None = None
    prior: bool = True  # the prior value as a hollow dot joined to the current one
    labels: bool = True


@visual(
    "dot_plot",
    DotPlotOptions,
    accepts=("rows",),
    min_size=(0.25, 0.2),
    check=_metric_check("dot_plot"),
)
def dot_plot(area: Area, result: Result, options: DotPlotOptions, ctx: DrawContext) -> None:
    """One dot per row for a metric (a share or a rate), the prior value as a hollow dot and the
    dot coloured by its change."""
    theme = area.theme
    metric = metric_of("dot_plot", result, options.metric, ctx.metric)
    pres = present(result)
    rows = _rows(result, pres)
    if not rows:
        return
    values = numbers(rows, metric)
    ax, yy = _category_axes(area, rows, ticks=not options.labels)
    prior = options.prior and pres.compare_ok and f"prior_{metric}" in pres.columns
    high = max(values + [0])
    if prior:
        prior_values = numbers(rows, f"prior_{metric}")
        ax.hlines(yy, prior_values, values, color=theme.color("grid"), lw=3)
        ax.scatter(
            prior_values,
            yy,
            s=26,
            facecolor=theme.color("card"),
            edgecolor=theme.color("muted"),
            lw=1.1,
            zorder=3,
        )
        high = max(high, *prior_values)
    change = next((c for c in (f"{metric}_delta", f"{metric}_change") if c in pres.columns), None)
    colors = [
        change_color(theme, r[change]) if prior and change else theme.color("accent") for r in rows
    ]
    alphas = [PALE if r.small else 1.0 for r in rows]
    for y, v, color, alpha in zip(yy, values, colors, alphas, strict=True):
        ax.scatter([v], [y], s=46, color=color, alpha=alpha, zorder=4)
    ax.set_xlim(0, max(high, 1e-9) * 1.2)
    ax.xaxis.set_major_formatter(_value_formatter(result, metric))
    if options.labels:
        for i, (y, r) in enumerate(zip(yy, rows, strict=True)):
            right = max(values[i], prior_values[i]) if prior else values[i]
            ax.annotate(
                r[metric].text,
                (right, y),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontsize=theme.size("label"),
                fontweight="semibold",
                color=text_color(theme, r[metric]),
            )
    entries = ([("○", "muted", "prior period")] if prior else []) + _small_entry(rows)
    if entries:
        legend(area, entries, 0.3, 0)


# --- stacked_share_bar ---------------------------------------------------------------------


class StackedShareBarOptions(Options):
    metric: str | None = None  # a share (format pct); default: the first share of the query
    details: list[str] = []  # metrics written under each segment's name


def _share_metric(result_or_query_metrics, formats_of) -> str | None:
    return next((m for m in result_or_query_metrics if formats_of(m) == "pct"), None)


def _stacked_check(options: StackedShareBarOptions, query, shape, registry) -> None:
    need_metrics("stacked_share_bar", "metric", [options.metric], query)
    need_metrics("stacked_share_bar", "details", options.details, query)
    have = query_metrics(query)
    if have is None:
        return
    metric = options.metric or _share_metric(have, lambda m: registry.metric(m).format)
    if metric is None or registry.metric(metric).format != "pct":
        raise VisualError(
            "visual stacked_share_bar: metric must be a share (format pct), such as share"
        )


@visual(
    "stacked_share_bar",
    StackedShareBarOptions,
    accepts=("rows",),
    min_size=(0.25, 0.12),
    check=_stacked_check,
)
def stacked_share_bar(
    area: Area, result: Result, options: StackedShareBarOptions, ctx: DrawContext
) -> None:
    """One bar split into the shares of the rows (payment types), with a legend of the names,
    their shares and the detail metrics."""
    theme = area.theme
    metrics = result.meta.value_metrics
    metric = options.metric or _share_metric(metrics, lambda m: metrics[m].format)
    if metric is None or metric not in result.df:
        raise VisualError("visual stacked_share_bar: the result has no share (format pct)")
    pres, short = present(result), present(result, "compact")
    rows = _rows(result, pres)
    texts = {r.position: r for r in short.rows}
    shares = [max(v, 0.0) for v in numbers(rows, metric)]
    bar_h = min(0.28, area.points_y(16))
    ax = area.axes(0, 1 - bar_h, 1, bar_h)
    left = 0.0
    for i, share in enumerate(shares):
        ax.barh(
            0,
            share,
            left=left,
            color=theme.series_color(i),
            height=1,
            edgecolor=theme.color("card"),
            lw=1.5,
        )
        left += share
    ax.set_xlim(0, max(left, 1))
    ax.axis("off")
    columns = 2 if len(rows) > 2 else max(len(rows), 1)
    step_y = area.points_y(theme.size("body") * 3.6)
    for i, r in enumerate(rows):
        fx, fy = (i % columns) / columns, 1 - bar_h - area.points_y(14) - (i // columns) * step_y
        dot = area.text(fx, fy, "●", role="body", color=theme.series_color(i), va="top")
        name = fits(r.label, area, "body", 1 / columns - 0.12)
        base.after(
            area.fig,
            dot,
            f"{name}  {r[metric].text}",
            4,
            fontsize=theme.size("body"),
            fontweight="semibold",
        )
        if options.details:
            detail = " · ".join(
                f"{texts[r.position][m].text} {result.meta.metrics[m].label.lower()}"
                for m in options.details
            )
            area.text(
                fx + area.points_x(12),
                fy - area.points_y(theme.size("body") * 1.5),
                detail,
                role="label",
                color="muted",
                va="top",
            )


# --- heatmap -------------------------------------------------------------------------------


class HeatmapOptions(Options):
    labels: bool = True  # the value in each cell
    color: Token = "accent"  # the colour of a full cell; a change is shaded good or bad


@visual("heatmap", HeatmapOptions, accepts=("matrix",), min_size=(0.35, 0.25))
def heatmap(area: Area, result: Result, options: HeatmapOptions, ctx: DrawContext) -> None:
    """A matrix (weekday × hour) as shaded cells: the larger the value, the darker; a change is
    shaded good or bad by its tone."""
    theme = area.theme
    pres = present(result, "compact")
    spec = result.meta.matrix
    columns = [c for c in pres.columns.values() if c.kind == "value"]
    values = np.array(
        [
            [np.nan if r[c.id].value is None else float(r[c.id].value) for c in columns]
            for r in pres.rows
        ]
    )
    finite = values[np.isfinite(values)]
    vmax = float(np.abs(finite).max()) if finite.size else 1.0
    is_change = spec.value != pres.metrics[0]
    card = np.array(to_rgba(theme.color("card")))
    rgba = np.zeros((*values.shape, 4))
    for (i, j), v in np.ndenumerate(values):
        if not np.isfinite(v) or vmax == 0:
            rgba[i, j] = card
            continue
        tone = pres.rows[i][columns[j].id].tone if is_change else None
        token = {"good": "good", "bad": "bad"}.get(tone or "", options.color)
        strength = 0.08 + 0.92 * abs(v) / vmax
        rgba[i, j] = card * (1 - strength) + np.array(to_rgba(theme.color(token))) * strength
    left = area.points_x(theme.size("label") * 5)
    bottom = area.points_y(theme.size("label") * 2.2)
    ax = area.axes(left, bottom, 1 - left, 1 - bottom)
    ax.imshow(rgba, aspect="auto", interpolation="nearest")
    ax.set_xticks(range(len(columns)), [c.label for c in columns])
    ax.set_yticks(range(len(pres.rows)), [r.label for r in pres.rows])
    ax.tick_params(labelsize=theme.size("label"), colors=theme.color("muted"), length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    if options.labels:
        for (i, j), v in np.ndenumerate(values):
            if not np.isfinite(v):
                continue
            strong = vmax and abs(v) / vmax > 0.55
            ax.text(
                j,
                i,
                pres.rows[i][columns[j].id].text,
                ha="center",
                va="center",
                fontsize=theme.size("small"),
                color=theme.color("card") if strong else theme.color("text"),
            )


# --- scatter_quadrant ----------------------------------------------------------------------


class ScatterQuadrantOptions(Options):
    x: str
    y: str
    classes: str | None = None  # the column of a `classify: {mode: quadrants}`; default: the first
    labels: bool = True  # name the points


def _scatter_check(options: ScatterQuadrantOptions, query, shape, registry) -> None:
    need_metrics("scatter_quadrant", "x", [options.x, options.y], query)


@visual(
    "scatter_quadrant",
    ScatterQuadrantOptions,
    accepts=("rows",),
    min_size=(0.3, 0.3),
    check=_scatter_check,
)
def scatter_quadrant(
    area: Area, result: Result, options: ScatterQuadrantOptions, ctx: DrawContext
) -> None:
    """Two metrics as a scatter, split into quadrants where `classify` split them (menu
    engineering), the points coloured by their class."""
    theme = area.theme
    pres = present(result)
    rows = _rows(result, pres)
    for m in (options.x, options.y):
        if m not in result.df:
            raise VisualError(f"visual scatter_quadrant: the result has no metric {m}")
    quadrants = [c for c in result.meta.classifications.values() if c.mode == "quadrants"]
    if options.classes is not None:
        quadrants = [c for c in quadrants if c.column == options.classes]
    classification = quadrants[0] if quadrants else None
    legend_h = area.points_y(LEGEND_POINTS)
    left = area.points_x(theme.size("label") * 5)
    bottom = legend_h + area.points_y(theme.size("label") * 2.5)
    ax = area.axes(left, bottom, 1 - left, 1 - bottom)
    _style(ax, area, grid=None)
    ax.spines["left"].set_visible(True)
    ax.spines["left"].set_color(theme.color("grid"))
    xs, ys = numbers(rows, options.x), numbers(rows, options.y)
    classes = list(classification.classes) if classification else []
    for r, x, y in zip(rows, xs, ys, strict=True):
        cls = result.df[classification.column].iloc[r.position] if classification else None
        color = theme.series_color(classes.index(cls)) if cls in classes else theme.color("muted")
        ax.scatter([x], [y], s=30, color=color, alpha=PALE if r.small else 1.0, zorder=3)
        if options.labels:
            ax.annotate(
                fits(r.label, area, "small", 0.18),
                (x, y),
                xytext=(4, 2),
                textcoords="offset points",
                fontsize=theme.size("small"),
                color=theme.color("muted" if r.small else "text"),
            )
    if classification is not None and {"x", "y"} <= classification.splits.keys():
        ax.axvline(classification.splits["x"], color=theme.color("grid"), lw=1, ls="--")
        ax.axhline(classification.splits["y"], color=theme.color("grid"), lw=1, ls="--")
    ax.xaxis.set_major_formatter(_value_formatter(result, options.x))
    ax.yaxis.set_major_formatter(_value_formatter(result, options.y))
    ax.set_xlabel(
        result.meta.metrics[options.x].label,
        fontsize=theme.size("label"),
        color=theme.color("muted"),
    )
    ax.set_ylabel(
        result.meta.metrics[options.y].label,
        fontsize=theme.size("label"),
        color=theme.color("muted"),
    )
    entries = [("●", theme.series_color(i), c) for i, c in enumerate(classes)]
    if entries or any(r.small for r in rows):
        legend(area, entries + _small_entry(rows), left, 0)


# --- waterfall -----------------------------------------------------------------------------


class WaterfallOptions(Options):
    labels: bool = True  # the effect above each step


NOT_SPLIT = "The change is not split: a value of a driver is zero or negative"


@visual("waterfall", WaterfallOptions, accepts=("decomposition",), min_size=(0.3, 0.25))
def waterfall(area: Area, result: Result, options: WaterfallOptions, ctx: DrawContext) -> None:
    """A bridge from the KPI's prior value to its current value through the effect of each
    driver (or member) of the decomposition."""
    theme = area.theme
    window = result.meta.window
    if not result.meta.compare_ok:
        label = window.compare_label if window is not None else None
        message = f"No valid comparison ({label})" if label else "No valid comparison"
        area.text(0, 0.5, message, color="muted", va="center")
        return
    pres = present(result)
    kpi = next(r for r in pres.rows if r.kind == "total")
    steps = [r for r in pres.rows if r.kind != "total" and result.df["level"].iloc[r.position] == 1]
    statuses = {result.df["status"].iloc[r.position] for r in steps}
    if "undefined" in statuses:
        steps = []
    prior, current = kpi["prior"].value or 0.0, kpi["current"].value or 0.0
    legend_h = area.points_y(LEGEND_POINTS)
    bottom = legend_h + area.points_y(theme.size("label") * 3.2)
    left = area.points_x(theme.size("label") * 4)
    ax = area.axes(left, bottom, 1 - left, 1 - bottom)
    _style(ax, area)
    labels = ["Prior"]
    tops = [prior]
    ax.bar(0, prior, width=0.6, color=theme.color("prior"))
    running = prior
    for i, r in enumerate(steps, start=1):
        effect = r["effect"].value or 0.0
        ax.bar(
            i,
            effect,
            bottom=running,
            width=0.6,
            color=change_color(theme, r["effect"]),
            alpha=PALE if r["effect"].small else 1.0,
        )
        if options.labels:
            ax.annotate(
                r["effect"].text,
                (i, max(running, running + effect)),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                fontsize=theme.size("label"),
                color=change_color(theme, r["effect"]),
                fontweight="semibold",
            )
        running += effect
        tops.append(max(running, running - effect))
        labels.append(fits(r.label, area, "label", 0.9 / (len(steps) + 2)))
    end = len(steps) + 1
    ax.bar(end, current, width=0.6, color=theme.color("accent"))
    labels.append("Now")
    tops.append(current)
    for x, cell in ((0, kpi["prior"]), (end, kpi["current"])):
        ax.annotate(
            cell.text,
            (x, cell.value or 0),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=theme.size("label"),
            fontweight="semibold",
        )
    for x in range(1, end):
        ax.plot(
            [x - 0.7, x + 0.3],
            [_level(prior, steps, x - 1)] * 2,
            color=theme.color("muted"),
            lw=0.6,
        )
    ax.set_xticks(range(end + 1), labels)
    ax.yaxis.set_major_formatter(_value_formatter(result, result.meta.decomposition.kpi))
    lows = [prior, current] + [_level(prior, steps, k) for k in range(len(steps) + 1)]
    floor = min(lows)
    span = max(tops) - floor or abs(max(tops)) or 1.0
    ax.set_ylim(max(floor - span * 0.6, 0), max(tops) + span * 0.25)
    entries = []
    if ax.get_ylim()[0] > 0:
        entries.append(("↕", "muted", "axis does not start at zero"))
    if "undefined" in statuses:
        entries.append(("!", "muted", NOT_SPLIT))
    if entries:
        legend(area, entries, left, 0)


def _level(prior: float, steps: list[Row], k: int) -> float:
    """The running value after the first k steps."""
    return prior + sum((r["effect"].value or 0.0) for r in steps[:k])
