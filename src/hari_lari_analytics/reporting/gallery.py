"""The gallery (plan 8.7): every component on one run, one page each.

The gallery is a report generated from the components, not a file: one page per component with a
visual, its slot as wide as the page and as high as the visual's `min_size` asks (at least the
height of a preview). It is the catalogue of what the framework can draw and a visual check:
a change to a visual, a transform or the theme shows on every page that uses it. The same
snapshots as `tests/snapshots/` give the parameters in the tests; the command

    hari-lari-report gallery [--run data/extract] [--anchor YYYY-MM-DD] [--out DIR]

builds it on a real run with the parameters the components have as defaults.

A component without a visual (a sheet of the workbook, such as `lines_export`) has no page, and
neither has one whose parameters have no default and are not given: both are returned as
`skipped` with the reason, so nothing disappears silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hari_lari_analytics.reporting import layouts, preview
from hari_lari_analytics.reporting.builder import BuildResult, build_report
from hari_lari_analytics.reporting.components import Component, ComponentError
from hari_lari_analytics.reporting.layouts import LayoutError
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.reports import (
    Report,
    ReportError,
    draws_own_card,
    facet_count,
    parse_report,
)
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME, Theme

REPORT_ID = "gallery"
COLUMNS = layouts.COLUMNS
ROWS = preview.ROWS


@dataclass(frozen=True)
class Skipped:
    component: str
    reason: str


@dataclass(frozen=True)
class Gallery:
    report: Report
    skipped: list[Skipped]

    @property
    def components(self) -> list[str]:
        return [p.id for p in self.report.spec.plain_pages()]


def slot_height(component: Component, params: Mapping[str, Any]) -> int:
    """The smallest height, in parts of the page body, at which the component fits its visual's
    minimum size: the preview's default or more. A ReportError names the component when even
    the whole page is too small."""
    bound = component.bind(dict(params))
    visual = bound.spec.visual
    assert visual is not None
    last = ""
    for height in range(preview.DEFAULT_HEIGHT, ROWS + 1):
        slot = preview.preview_slot(component, params, COLUMNS, height)
        try:
            layouts.check_size(
                slot.rect,
                slot.visual,
                not draws_own_card(visual.type),
                bool(bound.spec.hint),
                layouts.facet_cells(bound.spec.facet, facet_count(bound)),
                slot.ref.where,
            )
            return height
        except LayoutError as error:
            last = str(error)
    raise ReportError(f"{component.source}: does not fit a whole page: {last}")


def gallery_spec(
    components: Mapping[str, Component],
    params: Mapping[str, Mapping[str, Any]] | None = None,
    png: bool = True,
) -> tuple[dict, list[Skipped]]:
    """The raw report mapping of the gallery, and the components it leaves out. `params` gives
    a component's parameters by id (the snapshots' in the tests)."""
    params = params or {}
    pages = []
    skipped: list[Skipped] = []
    for cid in sorted(components):
        component = components[cid]
        own = dict(params.get(cid) or {})
        missing = sorted(component.unbound - set(own))
        if missing:
            skipped.append(Skipped(cid, f"no value for the parameter(s) {', '.join(missing)}"))
            continue
        try:
            bound = component.bind(own)
        except ComponentError as error:
            skipped.append(Skipped(cid, str(error)))
            continue
        if bound.spec.visual is None:
            skipped.append(Skipped(cid, "no visual"))
            continue
        slot: dict[str, Any] = {
            "component": cid,
            "row": 1,
            "col": 1,
            "span": COLUMNS,
            "height": slot_height(component, own),
        }
        if own:
            slot["params"] = own
        pages.append(
            {
                "id": cid,
                "title": cid,
                "grid": {"columns": COLUMNS, "rows": [1] * ROWS},
                "slots": [slot],
            }
        )
    raw = {
        "id": REPORT_ID,
        "title": "Gallery",
        "description": "Every component on one run, one page each",
        "kicker": "Gallery",
        "footer": "Gallery",
        "outputs": {"pdf": True, "png": "all" if png else []},
        "pages": pages,
    }
    return raw, skipped


def make_gallery(
    components: Mapping[str, Component],
    params: Mapping[str, Mapping[str, Any]] | None = None,
    png: bool = True,
) -> Gallery:
    """The gallery as a loaded report. It fails when no component has a page."""
    raw, skipped = gallery_spec(components, params, png)
    if not raw["pages"]:
        raise ReportError("the gallery has no page: no component has a visual to draw")
    return Gallery(parse_report(raw, "the gallery", components), skipped)


def build_gallery(
    components: Mapping[str, Component],
    ctx: QueryContext,
    out_dir: Path,
    params: Mapping[str, Mapping[str, Any]] | None = None,
    png: bool = True,
    generated: datetime | None = None,
    theme: Theme = DEFAULT_THEME,
) -> tuple[BuildResult, list[Skipped]]:
    gallery = make_gallery(components, params, png)
    return build_report(gallery.report, ctx, out_dir, generated, theme), gallery.skipped
