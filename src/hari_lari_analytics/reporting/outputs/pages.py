"""Drawing a page: the chrome, then every slot into its rectangle, then the overflow check.

The PDF and the PNG pages both draw from here, so they show the same page.
"""

from __future__ import annotations

from collections.abc import Sequence

from matplotlib.figure import Figure

from hari_lari_analytics.reporting import layouts, visuals
from hari_lari_analytics.reporting.builder import PageRun, ReportRun, SlotRun
from hari_lari_analytics.reporting.layouts import Overflow, Rect
from hari_lari_analytics.reporting.visuals import Area
from hari_lari_analytics.reporting.visuals.theme import Theme

EMPTY_PAGE_AREA: Rect = (layouts.LEFT, layouts.BOTTOM, layouts.RIGHT - layouts.LEFT, 0.78)


QUIET = "✓ nothing stands out"
NO_COMPARISON = "no comparison"


def draw_slot(fig: Figure, theme: Theme, slot: SlotRun, insights: Sequence) -> None:
    """One slot: the card with the component's title and hint, its facets inside, the note at
    the foot and, with `insights: chart`, the chart's sentences above the note (under the cells
    of a slot without a card) or, for a chart whose rules say nothing, the tag in the card's
    header row. `insights` are the page's sentences, for the notes visuals."""
    area = Area(fig, slot.slot.rect, theme)
    if slot.slot.card:
        area = visuals.card(area, slot.title, slot.hint)
    if slot.note:
        area = visuals.note(area, slot.note)
    if slot.draw_insights:
        if slot.insights:
            labelled = len(slot.facets) > 1
            area = visuals.foot(
                area,
                [(i.text, i.tone) for i in slot.insights],
                [i.facet or "" if labelled else "" for i in slot.insights],
            )
        elif slot.slot.card and slot.status in ("quiet", "no_comparison"):
            tag = QUIET if slot.status == "quiet" else NO_COMPARISON
            visuals.quiet_tag(fig, theme, slot.slot.rect, tag)
    spec = slot.slot.bound.spec
    visual = spec.visual
    layout = spec.facet.layout if spec.facet else "row"
    visuals.draw_facets(visual.type, visual.options, area, slot.facets, layout, insights)


def render_page(
    run: ReportRun, page: PageRun, theme: Theme, check_overflow: bool = True
) -> tuple[Figure, list[Overflow]]:
    """A page as a figure, and the texts that run over their slots."""
    fig = visuals.page(theme)
    layouts.draw_chrome(
        fig, theme, run.report.kicker, page.page.title, page.chips, page.note, run.footer
    )
    if page.page.empty_message is not None:
        visuals.card(Area(fig, EMPTY_PAGE_AREA, theme), page.page.empty_message)
        return fig, []
    drawn = []
    for slot in page.slots:
        before = layouts.texts_of(fig)
        draw_slot(fig, theme, slot, page.insights)
        drawn.append((slot.slot.ref.where, slot.slot.rect, layouts.drawn_texts(fig, before)))
    overflows = layouts.find_overflows(fig, drawn) if check_overflow else []
    return fig, overflows
