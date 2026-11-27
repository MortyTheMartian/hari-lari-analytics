"""Previews (plan 8.6): one component drawn at its default slot size, as a PNG.

`hari-lari-component run <id> --png` renders the component the way a report page would: into a
slot of a page grid, with the card, title and hint, from the same code as the PDF. The default
slot spans all 12 columns and half of the usable height of the page (3 of 6 row parts); `--span`
and `--height` change it. The image is cropped to the slot. `--watch` draws it again whenever
the component's file changes, so a component is written with the picture beside the editor.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

import matplotlib
from matplotlib.transforms import Bbox

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.presentation import layouts, visuals
from hari_lari_analytics.presentation.builder import run_slot
from hari_lari_analytics.presentation.components import Component
from hari_lari_analytics.presentation.insights import collect
from hari_lari_analytics.presentation.layouts import GridSpec, LayoutError, Overflow, SlotSpec
from hari_lari_analytics.presentation.outputs.pages import draw_slot
from hari_lari_analytics.presentation.outputs.png import PNG_DPI
from hari_lari_analytics.presentation.reports import (
    BoundRef,
    BoundSlot,
    ReportError,
    draws_own_card,
    facet_count,
    has_rules,
)
from hari_lari_analytics.presentation.visuals import page
from hari_lari_analytics.presentation.visuals.theme import DEFAULT_THEME, H, Theme, W

ROWS = 6  # the parts of the usable page height a slot's `height` counts in
DEFAULT_SPAN, DEFAULT_HEIGHT = 12, 3
MARGIN = 0.12  # inches around the slot in the image


def preview_slot(
    component: Component,
    params: Mapping[str, Any],
    span: int,
    height: int,
    sources: Sequence[Component] = (),
):
    """The slot a component is previewed in: row 1 of a grid of 6 equal rows. `sources` are the
    components of its `insights_from`."""
    if not 1 <= span <= layouts.COLUMNS:
        raise ReportError(f"--span: 1 to {layouts.COLUMNS} columns")
    if not 1 <= height <= ROWS:
        raise ReportError(f"--height: 1 to {ROWS} row parts")
    grid = GridSpec(rows=[1] * ROWS)
    spec = SlotSpec(component=component.id, row=1, col=1, span=span, height=height)
    rect = layouts.slot_rect(grid, spec)
    bound = component.bind(dict(params))
    visual = bound.spec.visual
    if visual is None:
        raise ReportError(f"{component.source}: the component has no visual to preview")
    card = not draws_own_card(visual.type)
    ref = BoundRef(component, dict(params), None, bound, (), f"preview of {component.id}")
    bound_sources = tuple(
        BoundRef(
            c,
            {k: v for k, v in params.items() if k in c.parameters},
            None,
            c.bind({k: v for k, v in params.items() if k in c.parameters}),
            (),
            f"insights_from {c.id} of {component.id}",
        )
        for c in sources
    )
    return BoundSlot(spec, rect, ref, visual.type, card, bound_sources)


def render_preview(
    component: Component,
    ctx: QueryContext,
    params: Mapping[str, Any],
    path: Path,
    span: int = DEFAULT_SPAN,
    height: int = DEFAULT_HEIGHT,
    theme: Theme = DEFAULT_THEME,
    sources: Sequence[Component] = (),
) -> list[Overflow]:
    """Draw a component into its slot and save the slot as a PNG, with its insight sentences as
    a report with `insights: chart` draws them. Returns the texts that run over the slot. A slot
    smaller than the visual's `min_size` is refused, as in a report."""
    slot = preview_slot(component, params, span, height, sources)
    cells = layouts.facet_cells(slot.bound.spec.facet, facet_count(slot.bound))
    hint = bool(slot.bound.spec.hint)
    # the sentences get their two lines when the slot has the room; a slot that only fits the
    # visual is drawn without them (a report gives the room in its grid)
    lines = visuals.FOOT_RESERVE_LINES if has_rules(slot.bound) else 0
    mode = "chart"
    try:
        layouts.check_size(slot.rect, slot.visual, slot.card, hint, cells, slot.ref.where, lines)
    except LayoutError as error:
        try:
            layouts.check_size(slot.rect, slot.visual, slot.card, hint, cells, slot.ref.where)
        except LayoutError:
            raise ReportError(f"{error} (--span {span}, --height {height})") from None
        mode = "none"
    run = run_slot(slot, ctx, insights=mode)
    with matplotlib.rc_context(theme.rc()):
        fig = page(theme)
        before = layouts.texts_of(fig)
        draw_slot(fig, theme, run, collect(run.insights))
        drawn = [(slot.ref.where, slot.rect, layouts.drawn_texts(fig, before))]
        overflows = layouts.find_overflows(fig, drawn)
        x, y, w, h = slot.rect
        box = Bbox.from_extents(
            x * W - MARGIN, y * H - MARGIN, (x + w) * W + MARGIN, (y + h) * H + MARGIN
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=PNG_DPI, bbox_inches=box)
    return overflows


def watch(
    target: Path,
    render: Callable[[], None],
    err: TextIO,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Call `render` now and again whenever `target` changes, until `stop()` (or Ctrl-C). A
    failure is printed and the loop goes on, so a typo in the YAML does not end the session."""

    def stamp() -> int:
        return target.stat().st_mtime_ns if target.exists() else 0

    def attempt() -> None:
        try:
            render()
        except ValueError as error:
            print(f"Error: {error}", file=err)

    last = stamp()
    attempt()
    try:
        while not stop():
            sleep(interval)
            if stamp() != last:
                last = stamp()
                attempt()
    except KeyboardInterrupt:
        print("", file=err)
