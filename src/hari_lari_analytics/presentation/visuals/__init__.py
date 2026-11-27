"""The visual registry: functions that draw a result into a rectangle, and never compute.

A visual is `(area, result, options, ctx) -> None`, registered with `@visual("name", ...)`:

- `options`: a pydantic model; unknown options fail. Options say what to show, never how
  (docs/components.md, "Visuals"): metrics, elements on or off, emphasis, colour tokens and
  text roles, never a point size or a hex colour.
- `accepts`: the result shapes it draws; `min_size`: (width, height) in fractions of the A4
  landscape page, for the layout engine (plan 8.4).
- `check(options, query, shape, registry)`: the component loader calls it, so an option that
  names a metric the query does not have fails when the component loads.
- `text(result, options, ctx)`: the plain-text form for the Telegram text, where there is one.
- `labels_facets`: the visual shows the facet's label itself (a KPI card's metric label);
  otherwise `draw_facets` writes it above the cell.

A result whose window has no sales is drawn as "No sales in this period" without calling the
visual. The visuals read display strings from `present.py`, never format numbers themselves.
"""

from __future__ import annotations

import math
import re
import textwrap
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.text import Text
from pydantic import BaseModel, ConfigDict, ValidationError

from hari_lari_analytics.analysis.compiler import Query
from hari_lari_analytics.analysis.queries import SourceQuery
from hari_lari_analytics.analysis.result import Result, Shape
from hari_lari_analytics.presentation.visuals import base
from hari_lari_analytics.presentation.visuals.theme import (
    DEFAULT_THEME,
    TOKENS,
    H,
    Role,
    Theme,
    Token,
    Tone,
    W,
)
from hari_lari_analytics.semantic.registry import Registry, RegistryError

ALL_SHAPES: tuple[Shape, ...] = ("single", "rows", "series", "matrix", "decomposition")
EMPTY_TEXT = "No sales in this period"
_GRID = re.compile(r"^grid\((\d+)\s*x\s*(\d+)\)$")
POINT = 1 / 72  # inches


class VisualError(ValueError):
    """A visual is unknown, its options are wrong, or it cannot draw this result."""


class Options(BaseModel):
    """Base of the options models: an unknown option is an error, as everywhere in the YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoOptions(Options):
    pass


@dataclass(frozen=True)
class Area:
    """The rectangle a visual may draw in, in fractions of the figure: (x, y, width, height).

    Positions given to its helpers are fractions of the area, from its lower left corner."""

    fig: Figure
    rect: tuple[float, float, float, float]
    theme: Theme = DEFAULT_THEME

    @property
    def x(self) -> float:
        return self.rect[0]

    @property
    def y(self) -> float:
        return self.rect[1]

    @property
    def w(self) -> float:
        return self.rect[2]

    @property
    def h(self) -> float:
        return self.rect[3]

    @property
    def inches(self) -> tuple[float, float]:
        fw, fh = self.fig.get_size_inches()
        return self.w * fw, self.h * fh

    def at(self, fx: float, fy: float) -> tuple[float, float]:
        """A position in the area as a position in the figure."""
        return self.x + fx * self.w, self.y + fy * self.h

    def points_y(self, points: float) -> float:
        """A height in points as a fraction of the area's height."""
        return points * POINT / self.inches[1]

    def points_x(self, points: float) -> float:
        return points * POINT / self.inches[0]

    def sub(self, fx: float, fy: float, fw: float, fh: float) -> Area:
        x, y = self.at(fx, fy)
        return Area(self.fig, (x, y, fw * self.w, fh * self.h), self.theme)

    def axes(self, fx: float = 0, fy: float = 0, fw: float = 1, fh: float = 1, **kw) -> Axes:
        x, y = self.at(fx, fy)
        return self.fig.add_axes((x, y, fw * self.w, fh * self.h), **kw)

    def text(
        self,
        fx: float,
        fy: float,
        s: str,
        role: Role = "body",
        color: Token | str = "text",
        weight: str = "normal",
        **kw,
    ) -> Text:
        """Text at a position of the area; `color` is a token, or a colour the theme resolved."""
        x, y = self.at(fx, fy)
        resolved = self.theme.color(color) if color in TOKENS else color
        return self.fig.text(
            x, y, s, fontsize=self.theme.size(role), fontweight=weight, color=resolved, **kw
        )

    def split(self, n: int, layout: str = "row", gap_points: float = 8) -> list[Area]:
        """`n` cells: side by side (`row`), stacked (`column`) or a grid(<rows>x<columns>),
        filled row by row."""
        m = _GRID.match(layout)
        rows, cols = (int(m[1]), int(m[2])) if m else (1, n) if layout == "row" else (n, 1)
        gx, gy = self.points_x(gap_points), self.points_y(gap_points)
        cw, ch = (1 - gx * (cols - 1)) / cols, (1 - gy * (rows - 1)) / rows
        cells = []
        for i in range(n):
            r, c = divmod(i, cols)
            cells.append(self.sub(c * (cw + gx), 1 - (r + 1) * ch - r * gy, cw, ch))
        return cells


@dataclass(frozen=True)
class DrawContext:
    theme: Theme = DEFAULT_THEME
    metric: str | None = None  # the metric of a facet by metric
    label: str | None = None  # the facet's label
    insights: Sequence[Any] = ()  # the sentences, for the insights visual


Fn = Callable[[Area, Result, Any, DrawContext], None]
Check = Callable[[Any, "Query | SourceQuery", "Shape | None", Registry], None]
TextFn = Callable[[Result, Any, DrawContext], list[str]]


@dataclass(frozen=True)
class Visual:
    name: str
    fn: Fn
    options: type[BaseModel]
    accepts: tuple[Shape, ...]
    min_size: tuple[float, float]
    check: Check | None
    text: TextFn | None
    labels_facets: bool
    description: str


VISUALS: dict[str, Visual] = {}


def visual(
    name: str,
    options: type[BaseModel] = NoOptions,
    accepts: Iterable[Shape] = ("rows",),
    min_size: tuple[float, float] = (0.2, 0.15),
    check: Check | None = None,
    text: TextFn | None = None,
    labels_facets: bool = False,
) -> Callable[[Fn], Fn]:
    """Register a visual under `name`; its docstring is its description."""

    def register(fn: Fn) -> Fn:
        if name in VISUALS:
            raise ValueError(f"the visual {name} is registered twice")
        doc = (fn.__doc__ or "").strip().splitlines()
        VISUALS[name] = Visual(
            name,
            fn,
            options,
            tuple(accepts),
            min_size,
            check,
            text,
            labels_facets,
            doc[0] if doc else "",
        )
        return fn

    return register


def get(name: str) -> Visual:
    if name not in VISUALS:
        raise VisualError(f"unknown visual {name!r} (known: {', '.join(sorted(VISUALS))})")
    return VISUALS[name]


def parse_options(name: str, raw: dict) -> BaseModel:
    spec = get(name)
    try:
        return spec.options.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'options'}: "
            f"{e['msg'].removeprefix('Value error, ')}"
            for e in error.errors()
        )
        raise VisualError(f"visual {name}: {details}") from None


def check_visual(
    name: str,
    raw: dict,
    query: Query | SourceQuery,
    shape: Shape | None,
    registry: Registry,
) -> BaseModel:
    """Check a component's visual without data: known, its options, the result shape (unknown
    for a Python source) and its own check against the query."""
    spec = get(name)
    options = parse_options(name, raw)
    if shape is not None and shape not in spec.accepts:
        raise VisualError(
            f"visual {name} does not draw a result of shape {shape} "
            f"(it accepts {', '.join(spec.accepts)})"
        )
    if spec.check is not None:
        try:
            spec.check(options, query, shape, registry)
        except RegistryError as error:
            raise VisualError(f"visual {name}: {error}") from None
    return options


# --- helpers for the checks of the visuals ------------------------------------------------------


def query_metrics(query: Query | SourceQuery) -> list[str] | None:
    """The metrics of a compiled query; None for a Python source (whose columns are known only
    when it runs)."""
    return list(query.metrics) if isinstance(query, Query) else None


def need_metrics(
    name: str, option: str, metrics: Iterable[str | None], query: Query | SourceQuery
) -> None:
    have = query_metrics(query)
    if have is None:
        return
    missing = [m for m in metrics if m is not None and m not in have]
    if missing:
        raise VisualError(
            f"visual {name}: {option}: {', '.join(missing)} is not a metric of the query "
            f"({', '.join(have)})"
        )


def need_compare(name: str, option: str, query: Query | SourceQuery) -> None:
    if isinstance(query, Query) and query.compare == "none":
        raise VisualError(f"visual {name}: {option} needs a comparison: set `compare` on the query")


# --- drawing ------------------------------------------------------------------------------------


def draw(
    name: str,
    raw_options: dict | BaseModel | None,
    area: Area,
    result: Result,
    ctx: DrawContext | None = None,
) -> None:
    """Draw one result with a visual, after checking its options and the result's shape."""
    spec = get(name)
    ctx = ctx or DrawContext(theme=area.theme)
    options = (
        raw_options
        if isinstance(raw_options, BaseModel)
        else parse_options(name, raw_options or {})
    )
    if result.meta.empty and result.meta.window is not None:
        area.text(0, 0.5, EMPTY_TEXT, role="body", color="muted", va="center")
        return
    if result.meta.shape not in spec.accepts:
        raise VisualError(
            f"visual {name} does not draw a result of shape {result.meta.shape} "
            f"(it accepts {', '.join(spec.accepts)})"
        )
    spec.fn(area, result, options, ctx)


def plain_text(
    name: str, raw_options: dict | None, result: Result, ctx: DrawContext | None = None
) -> list[str]:
    """The plain-text lines of a result, for the visuals that have a text form."""
    spec = get(name)
    if spec.text is None:
        raise VisualError(f"visual {name} has no text form")
    if result.meta.empty and result.meta.window is not None:
        return [EMPTY_TEXT]
    return spec.text(result, parse_options(name, raw_options or {}), ctx or DrawContext())


def draw_facets(
    name: str,
    raw_options: dict | None,
    area: Area,
    facets: Sequence[Any],
    layout: str = "row",
    insights: Sequence[Any] = (),
) -> None:
    """Draw the facets of a slot (`facets.Facet`: result, label, metric), one per cell of the
    layout; a facet's label goes above its cell unless the visual shows it itself."""
    spec = get(name)
    options = parse_options(name, raw_options or {})
    if not facets:
        area.text(0, 0.5, EMPTY_TEXT, role="body", color="muted", va="center")
        return
    cells = area.split(len(facets), layout) if len(facets) > 1 else [area]
    for cell, facet in zip(cells, facets, strict=True):
        ctx = DrawContext(area.theme, facet.metric, facet.label, insights)
        if facet.label and not spec.labels_facets and len(facets) > 1:
            cell.text(0, 1, facet.label, role="body", weight="semibold", va="top")
            label_h = cell.points_y(area.theme.size("body") * 1.8)
            cell = cell.sub(0, 0, 1, max(1 - label_h, 0.05))
        draw(name, options, cell, facet.result, ctx)


CARD_PAD = (0.014, 0.02)  # inside a card, as today's report: left and right, bottom
CARD_HEAD = {"none": 0.02, "title": 0.05, "hint": 0.072}  # from the card's top to the visual


def card(area: Area, title: str = "", hint: str = "") -> Area:
    """Draw a card over the area with the title and hint, as today's report does, and return the
    area left for the visual."""
    base.card(area.fig, area.theme.palette, *area.rect, title, hint)
    head = CARD_HEAD["hint" if hint else "title" if title else "none"]
    px, py = CARD_PAD
    rect = (area.x + px, area.y + py, area.w - 2 * px, max(area.h - head - py, 0.01))
    return Area(area.fig, rect, area.theme)


NOTE_LINESPACING = 1.6


def note(area: Area, text: str) -> Area:
    """Write a note at the foot of the area, wrapped to its width (a line break in the text is
    kept), and return the area left above it."""
    lines = [line for part in text.strip().split("\n") for line in wrap(part, area, "hint")]
    size = area.theme.size("hint")
    area.text(
        0,
        0,
        "\n".join(lines),
        role="hint",
        color="muted",
        va="bottom",
        linespacing=NOTE_LINESPACING,
    )
    used = area.points_y(size * (NOTE_LINESPACING * len(lines) + 1))
    return area.sub(0, used, 1, max(1 - used, 0.05))


FOOT_STEP = 1.5  # the line step of the sentences, in hint sizes
FOOT_RESERVE_LINES = 2  # what `check_size` keeps free for a chart's sentences


def foot_points(lines: int, theme: Theme = DEFAULT_THEME) -> float:
    """The height of `lines` lines of sentences at the foot of a card, with the rule above
    them, in points."""
    return theme.size("hint") * (FOOT_STEP * lines + 0.8)


def foot(
    area: Area, sentences: Sequence[tuple[str, Tone | None]], prefixes: Sequence[str] = ()
) -> Area:
    """The insight sentences of a chart at the foot of the area (`hint` size, a dot in the
    tone's colour, wrapped to the width, a thin rule above them); returns the area above.

    `sentences` are (text, tone); a prefix (the facet label of a faceted chart) goes before
    its sentence."""
    theme = area.theme
    mark_w = area.points_x(theme.size("hint") * 1.4)
    lines: list[tuple[str, str, str]] = []
    for i, (text, tone) in enumerate(sentences):
        prefix = prefixes[i] if i < len(prefixes) else ""
        full = f"{prefix}: {text}" if prefix else text
        for k, line in enumerate(wrap(full, area, "hint", 0.97 - mark_w)):
            lines.append(("●" if k == 0 else "", theme.tone_color(tone), line))
    step = area.points_y(theme.size("hint") * FOOT_STEP)
    y = step * (len(lines) - 1)
    for mark, color, line in lines:
        area.text(0, y, mark, role="hint", color=color, va="bottom")
        area.text(mark_w, y, line, role="hint", color="text", va="bottom")
        y -= step
    used = area.points_y(foot_points(len(lines), theme))
    rule_y = used - area.points_y(theme.size("hint") * 0.4)
    x0, y0 = area.at(0, rule_y)
    x1, _ = area.at(1, rule_y)
    area.fig.add_artist(Line2D([x0, x1], [y0, y0], color=theme.color("grid"), lw=0.6))
    return area.sub(0, used, 1, max(1 - used, 0.05))


def quiet_tag(
    fig: Figure, theme: Theme, rect: tuple[float, float, float, float], text: str
) -> None:
    """The tag in the header row of a card (its rectangle on the page): the right end, `hint`
    size, muted."""
    x, y, w, h = rect
    fig.text(
        x + w - CARD_PAD[0],
        y + h - 0.018,
        text,
        fontsize=theme.size("hint"),
        color=theme.color("muted"),
        ha="right",
        va="top",
    )


def page(theme: Theme = DEFAULT_THEME) -> Figure:
    """An empty A4 landscape page in the theme."""
    fig = Figure(figsize=(W, H))
    fig.set_facecolor(theme.color("bg"))
    return fig


def fits(text: str, area: Area, role: Role, width: float = 1.0) -> str:
    """`text` shortened with … so that it fits about `width` of the area at this role's size;
    an estimate from the average width of a character (about half the font size)."""
    chars = max(int(area.inches[0] * width / (area.theme.size(role) * 0.52 * POINT)), 4)
    return text if len(text) <= chars else text[: chars - 1].rstrip() + "…"


def wrap(text: str, area: Area, role: Role, width: float = 1.0) -> list[str]:
    """`text` broken into lines that fit about `width` of the area (the estimate of `fits`)."""
    chars = max(int(area.inches[0] * width / (area.theme.size(role) * 0.52 * POINT)), 8)
    return textwrap.wrap(text, chars) or [""]


def nice_ceiling(value: float) -> float:
    """A round upper limit for an axis."""
    if value <= 0 or math.isnan(value):
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exp:
            return step * exp
    return 10 * exp


from hari_lari_analytics.presentation.visuals import (  # noqa: E402, F401
    charts,
    kpi,
    notes,
    tables,
)
