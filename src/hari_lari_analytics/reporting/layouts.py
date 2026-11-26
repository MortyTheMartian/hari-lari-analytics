"""The grid layout engine (plan 8.4): where a slot goes on a page, and whether it fits.

A page is a 12-column grid with row heights in parts of the usable height (docs/components.md,
"Layouts"). A slot names a component with its `row`, `col`, column `span` and row `height`. The
usable area is the page without its header and footer, the same area today's pages use, with a
fixed gap between cells.

```yaml
grid: { columns: 12, rows: [2, 5] }
slots:
  - { component: kpi_cards,          row: 1, col: 1, span: 12 }
  - { component: sales_orders_trend, row: 2, col: 1, span: 8 }
  - { component: key_figures,        row: 2, col: 9, span: 4 }
```

What the engine does:

- `place` turns the slots into rectangles (fractions of the page, from its lower left corner).
- `check_slots` refuses a slot outside the grid and two slots that overlap.
- `check_size` refuses a slot too small for its visual: the area inside the card, split into the
  cells of the slot's facets, must reach the visual's `min_size`.
- `draw_chrome` draws what every page has, from the report context: the kicker, the title, the
  header chips, the note and the footer.
- `find_overflows` measures every text a slot drew and reports the ones that reach outside the
  slot (the test of the report builder fails on any).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.text import Text
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hari_lari_analytics.reporting import visuals
from hari_lari_analytics.reporting.facets import FacetSpec
from hari_lari_analytics.reporting.visuals import CARD_HEAD, CARD_PAD, Area, base
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME, H, Theme, W

Rect = tuple[float, float, float, float]  # x, y, width, height; fractions of the page

COLUMNS = 12
LEFT, RIGHT = 0.035, 0.965  # the usable width of a page
BOTTOM, TOP = 0.07, 0.855  # the usable height: under the header, above the footer
GAP_X, GAP_Y = 0.0133, 0.02  # between cells
FACET_GAP_POINTS = 8  # between the cells of a facet layout (Area.split)
OVERFLOW_TOLERANCE = 0.003  # of the page: anti-aliasing and the extent of a glyph's box


class LayoutError(ValueError):
    """A slot does not fit the grid or its visual."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class GridSpec(_Strict):
    """The grid of a page: 12 columns, and the heights of the rows in parts of the usable
    height (`[2, 5]`: the first row gets two sevenths)."""

    columns: Literal[12] = COLUMNS
    rows: list[float] = Field(min_length=1)

    @field_validator("rows")
    @classmethod
    def _positive(cls, rows: list[float]) -> list[float]:
        if any(r <= 0 for r in rows):
            raise ValueError("a row height is a positive number of parts")
        return rows


class SlotSpec(_Strict):
    """One component on a page. `with` overrides fields of the component (a deep merge),
    `params` binds its parameters, `card: false` leaves out the card with title and hint, and
    `ignore_filters` opts the slot out of the filters of the report and the page."""

    component: str
    row: int = Field(ge=1)
    col: int = Field(ge=1, le=COLUMNS)
    span: int = Field(default=COLUMNS, ge=1, le=COLUMNS)
    height: int = Field(default=1, ge=1)
    with_: dict | None = Field(default=None, alias="with")
    params: dict = {}
    card: bool | None = None  # default: a card, unless the visual draws its own
    ignore_filters: bool | list[str] = False


@dataclass(frozen=True)
class Placement:
    slot: SlotSpec
    rect: Rect


# --- geometry -------------------------------------------------------------------------------------


def _row_heights(grid: GridSpec) -> list[float]:
    room = (TOP - BOTTOM) - GAP_Y * (len(grid.rows) - 1)
    parts = sum(grid.rows)
    return [room * r / parts for r in grid.rows]


def slot_rect(grid: GridSpec, slot: SlotSpec) -> Rect:
    """The rectangle of a slot, in fractions of the page."""
    col_w = ((RIGHT - LEFT) - GAP_X * (COLUMNS - 1)) / COLUMNS
    x = LEFT + (slot.col - 1) * (col_w + GAP_X)
    w = slot.span * col_w + (slot.span - 1) * GAP_X
    heights = _row_heights(grid)
    first, last = slot.row - 1, slot.row - 1 + slot.height - 1
    last = min(last, len(heights) - 1)
    top = TOP - sum(heights[:first]) - first * GAP_Y
    h = sum(heights[first : last + 1]) + (last - first) * GAP_Y
    return (x, top - h, w, h)


def check_slots(grid: GridSpec, slots: Sequence[SlotSpec], where: str = "page") -> None:
    """Every slot inside the grid, none on another."""
    problems = []
    for i, s in enumerate(slots, start=1):
        label = f"{where}: slot {i} ({s.component})"
        if s.col + s.span - 1 > COLUMNS:
            problems.append(f"{label}: col {s.col} and span {s.span} run past column {COLUMNS}")
        if s.row + s.height - 1 > len(grid.rows):
            problems.append(
                f"{label}: row {s.row} and height {s.height} run past the {len(grid.rows)} "
                "row(s) of the grid"
            )
    for i, a in enumerate(slots, start=1):
        for j, b in enumerate(slots[i:], start=i + 1):
            if (
                a.col < b.col + b.span
                and b.col < a.col + a.span
                and a.row < b.row + b.height
                and b.row < a.row + a.height
            ):
                problems.append(
                    f"{where}: slot {i} ({a.component}) and slot {j} ({b.component}) overlap"
                )
    if problems:
        raise LayoutError("\n".join(problems))


def place(grid: GridSpec, slots: Sequence[SlotSpec], where: str = "page") -> list[Placement]:
    """The slots with their rectangles, after `check_slots`."""
    check_slots(grid, slots, where)
    return [Placement(s, slot_rect(grid, s)) for s in slots]


# --- sizes ----------------------------------------------------------------------------------------


def inner_size(rect: Rect, card: bool, hint: bool, foot_lines: int = 0) -> tuple[float, float]:
    """The width and height (fractions of the page) a visual gets inside a slot: the slot
    without its card's padding and head, and without the `foot_lines` lines of sentences a chart
    of a report with `insights: chart` keeps free at its foot."""
    foot = visuals.foot_points(foot_lines) * visuals.POINT / H if foot_lines else 0
    if not card:
        return rect[2], rect[3] - foot
    px, py = CARD_PAD
    head = CARD_HEAD["hint" if hint else "title"]
    return rect[2] - 2 * px, rect[3] - head - py - foot


def facet_cells(facet: FacetSpec | None, count: int | None = None) -> tuple[int, int]:
    """(rows, columns) of the cells a slot's facets are drawn in. A row or column layout with
    an unknown number of facets counts as one."""
    if facet is None:
        return 1, 1
    if facet.grid is not None:
        return facet.grid
    n = count if count is not None else 1
    return (1, n) if facet.layout == "row" else (n, 1)


def check_size(
    rect: Rect,
    visual: str,
    card: bool,
    hint: bool,
    cells: tuple[int, int] = (1, 1),
    where: str = "slot",
    foot_lines: int = 0,
) -> None:
    """Refuse a slot too small for its visual; each facet cell must reach the `min_size`. A
    slot with `foot_lines` lines of sentences has that much less room."""
    min_w, min_h = visuals.get(visual).min_size
    w, h = inner_size(rect, card, hint, foot_lines)
    rows, cols = cells
    gx = (FACET_GAP_POINTS / 72) / W * (cols - 1)
    gy = (FACET_GAP_POINTS / 72) / H * (rows - 1)
    cell_w, cell_h = (w - gx) / cols, (h - gy) / rows
    if cell_w + 1e-9 < min_w or cell_h + 1e-9 < min_h:
        split = f" in each of {rows * cols} cells" if rows * cols > 1 else ""
        raise LayoutError(
            f"{where}: the visual {visual} needs at least {min_w:.0%} × {min_h:.0%} of the page"
            f"{split} but the slot gives {max(cell_w, 0):.0%} × {max(cell_h, 0):.0%}: "
            "give it more columns or rows"
        )


# --- the chrome -----------------------------------------------------------------------------------


def draw_chrome(
    fig: Figure,
    theme: Theme,
    kicker: str,
    title: str,
    chips: Sequence[tuple[str, str]] = (),
    note: str = "",
    footer: str = "",
) -> None:
    """The header (kicker, title, chips and an optional note) and the footer of a page, as on
    today's pages. A chip's style is `fill`, `line` or `muted`."""
    page = Area(fig, (0, 0, 1, 1), theme)
    page.text(LEFT, 0.935, kicker.upper(), role="kicker", color="accent", weight="semibold")
    page.text(LEFT, 0.895, title, role="page_title", weight="bold")
    base.chips(fig, theme.palette, list(chips))
    if note:
        page.text(RIGHT, 0.872, note, role="small", color="muted", ha="right")
    if footer:
        page.text(LEFT, 0.025, footer, role="small", color="muted")


# --- overflow -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Overflow:
    """A text that reaches outside its slot."""

    where: str  # the slot, as named by the caller
    text: str
    sides: tuple[str, ...]  # left, right, top, bottom
    by: float  # how far it reaches out at most, as a fraction of the page

    def describe(self) -> str:
        return f"{self.where}: {self.text!r} runs over the {'/'.join(self.sides)} edge"


def texts_of(fig: Figure) -> set[int]:
    """The identity of every text artist on a figure now (to tell later which are new)."""
    return {id(t) for t in fig.findobj(Text)}


def drawn_texts(fig: Figure, before: set[int]) -> list[Text]:
    """The texts that were added to the figure since `before` and will show: visible, not
    empty and not on an axes that is switched off."""
    found = []
    for t in fig.findobj(Text):
        if id(t) in before or not t.get_visible() or not t.get_text().strip():
            continue
        axes = getattr(t, "axes", None)
        if axes is not None and not axes.axison:
            continue
        found.append(t)
    return found


def find_overflows(
    fig: Figure,
    drawn: Iterable[tuple[str, Rect, Sequence[Text]]],
    tolerance: float = OVERFLOW_TOLERANCE,
) -> list[Overflow]:
    """The texts that reach outside their slot. `drawn` lists, per slot, its name, rectangle
    and the texts it drew. The figure is rendered once to measure the texts."""
    canvas = fig.canvas if isinstance(fig.canvas, FigureCanvasAgg) else FigureCanvasAgg(fig)
    canvas.draw()
    renderer = canvas.get_renderer()
    width, height = fig.bbox.width, fig.bbox.height
    found: list[Overflow] = []
    for where, (x, y, w, h), texts in drawn:
        for t in texts:
            box = t.get_window_extent(renderer)
            left, right = box.x0 / width, box.x1 / width
            bottom, top = box.y0 / height, box.y1 / height
            sides = {
                "left": x - left,
                "right": right - (x + w),
                "bottom": y - bottom,
                "top": top - (y + h),
            }
            out = {side: by for side, by in sides.items() if by > tolerance}
            if out:
                found.append(Overflow(where, t.get_text(), tuple(out), max(out.values())))
    return found


def new_page(theme: Theme = DEFAULT_THEME) -> Figure:
    """An empty A4 landscape page in the theme."""
    return visuals.page(theme)
