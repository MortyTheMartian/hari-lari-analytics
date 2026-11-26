"""The report builder (plan 8.5): run a report on one run and write its outputs.

`build_report` is `run_report` (the data: expand the report into pages, resolve the repeats
against the data, run every slot's component and its facets, collect the insights, render the
text blocks) followed by `write_outputs` (the PDF, the PNG pages, the Telegram text, the
workbook and the JSON bundle, each in `outputs/`). Delivering the files is a separate step, so
building never sends anything.

The page chrome comes from the report context: the header chips show the page's window and the
days of its comparison (and the filters in force), and the note says when the comparison starts
before the first sale.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import jinja2

from hari_lari_analytics.project import project
from hari_lari_analytics.reporting import visuals
from hari_lari_analytics.reporting.compiler import Query, TopSpec, run_query
from hari_lari_analytics.reporting.components import render_text
from hari_lari_analytics.reporting.facets import Facet, _python, run_facets
from hari_lari_analytics.reporting.insights import (
    CHART_MAX,
    Insight,
    InsightContext,
    collect,
    component_insights,
)
from hari_lari_analytics.reporting.layouts import Overflow
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.reports import (
    DRIVE_PLACEHOLDER,
    DRIVE_URL_LENGTH,
    AppliedFilter,
    BoundRef,
    BoundSlot,
    InsightsItem,
    Report,
    ReportError,
    ReportPage,
    Resolver,
    TextLine,
    TopRepeat,
    bind_refs,
    context,
    expand,
    render,
    top_model,
)
from hari_lari_analytics.reporting.visuals import DrawContext
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME, Theme
from hari_lari_analytics.reporting.windows import parse_window, resolve

Chip = tuple[str, str]  # text, style: fill, line or muted


class BuildError(ReportError):
    """A report cannot be built from this run, for example a text that is too long."""


# --- resolving what the data decides --------------------------------------------------------------


class DataResolver(Resolver):
    """The values of a repeat over `top` and the empty windows, from the data of a run."""

    def __init__(self, ctx: QueryContext):
        self.ctx = ctx

    def values(self, name: str, spec: Any, params: Mapping[str, Any], report: Report) -> list[Any]:
        if not isinstance(spec, TopRepeat):
            return list(spec)
        registry = self.ctx.registry
        model = top_model(registry, spec)
        window = str(params[spec.window[1:]]) if spec.window.startswith("$") else spec.window
        where = {}
        for dimension, value in report.spec.filters.where.items():
            try:
                registry.dimension(dimension).column_on(model)
            except Exception:
                continue
            where[dimension] = _bound(value, params)
        query = Query(
            model=model,
            metrics=[spec.by],
            dimensions=[spec.dimension],
            window=window,
            where=where,
            top=TopSpec(n=spec.top, by=spec.by),
        )
        result = run_query(self.ctx.con, query, registry, self.ctx.calendar)
        return [_python(v) for v in result.rows[spec.dimension]]

    def is_empty(self, window: str, params: Mapping[str, Any]) -> bool:
        query = Query(model="fct_sales_lines", metrics=["net_sales"], window=window)
        return run_query(self.ctx.con, query, self.ctx.registry, self.ctx.calendar).meta.empty


def _bound(value: Any, params: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$") and value[1:] in params:
        return params[value[1:]]
    return value


# --- what a build produces ------------------------------------------------------------------------


InsightStatus = Literal["found", "quiet", "no_comparison"]


@dataclass(frozen=True)
class SlotRun:
    slot: BoundSlot
    title: str
    hint: str
    note: str
    facets: list[Facet]
    # the chart's own sentences: those of its rules and of its `insights_from`, the best per
    # facet and key, at most CHART_MAX, best first
    insights: list[Insight]
    # everything the rules found, before the cut: what the page's sentences are collected from
    candidates: list[Insight] = field(default_factory=list)
    # None: no rules, or no sales to speak about
    status: InsightStatus | None = None
    draw_insights: bool = False  # the report says `insights: chart`: draw them under the chart


@dataclass(frozen=True)
class PageRun:
    page: ReportPage
    chips: list[Chip]
    note: str
    slots: list[SlotRun]
    insights: list[Insight]  # the page's sentences: collected, ranked and de-duplicated


@dataclass(frozen=True)
class SheetRun:
    """A component of the workbook with its results."""

    ref: BoundRef
    facets: list[Facet]


@dataclass(frozen=True)
class ReportRun:
    report: Report
    anchor: date
    generated: datetime
    pages: list[PageRun]
    texts: dict[str, str]  # the Telegram text blocks, with the Drive link still a placeholder
    sheets: list[SheetRun]
    ctx: QueryContext = field(repr=False, compare=False)

    @property
    def footer(self) -> str:
        return (
            f"{self.report.footer} · data through {self.anchor:%d %b %Y} · "
            f"generated {self.generated:%d %b %Y %H:%M}"
        )


# --- the page chrome ------------------------------------------------------------------------------


def filter_chip(ctx: QueryContext, f: AppliedFilter) -> Chip:
    """A filter in force as a chip: `Payment type: Cash`."""
    try:
        dim = ctx.registry.dimension(f.dimension)
        label = dim.label
        values = f.value if isinstance(f.value, list) else [f.value]
        shown = ", ".join(dim.value_label(v) for v in values)
    except Exception:
        label, shown = f.dimension, str(f.value)
    return f"{label}: {shown}" if f.kind == "where" else f"{label}: {shown} (focus)", "line"


def page_chrome(ctx: QueryContext, page: ReportPage) -> tuple[list[Chip], str]:
    """The header chips of a page and its note, from the page's window and filters."""
    chips: list[Chip] = []
    note = ""
    if page.period is not None:
        kind, _ = parse_window(page.period)
        window = resolve(page.period, ctx.calendar, "none" if kind == "full" else "prior")
        chips += [(window.current.title, "fill"), (window.current.label, "line")]
        if window.compare != "none":
            if window.compare_ok:
                chips.append((f"vs {window.comparison.label}", "muted"))
            else:
                chips.append(("no comparison", "muted"))
                first = ctx.calendar.first_sale
                note = (
                    f"The prior period starts before the first sale ({first:%d %b %Y})."
                    if first
                    else ""
                )
    chips += [filter_chip(ctx, f) for f in page.filters]
    return chips, note


# --- running a slot -------------------------------------------------------------------------------


def slot_texts(ref: BoundRef, facets: Sequence[Facet]) -> tuple[str, str, str]:
    """The rendered title, hint and note of a component: from its first facet's result, or
    without `meta` when the window has no sales."""
    spec = ref.bound.spec
    params = ref.bound.params
    first = facets[0] if facets else None
    meta = first.result.meta if first else None
    templates = (spec.title, spec.hint, spec.note)
    try:
        title, hint, note = (render_text(t, params, meta, first) for t in templates)
    except jinja2.TemplateError:
        title, hint, note = (render_text(t, params, meta, first, strict=False) for t in templates)
    return title, hint, note


def unfiltered_note(ctx: QueryContext, slot: BoundSlot, in_force: Sequence[AppliedFilter]) -> str:
    """For the hint of a slot: the filters of the report and page that do not reach it (its
    model lacks the dimension, or it opts out), so a printed page never looks filtered where it
    is not. A dimension the slot filters itself is not named."""
    applied = {(f.kind, f.dimension) for f in slot.ref.filters}
    query = slot.bound.query
    own = {*getattr(query, "where", {}), *getattr(query, "focus", {})}
    missed = []
    for f in in_force:
        if (f.kind, f.dimension) in applied or f.dimension in own:
            continue
        try:
            label = ctx.registry.dimension(f.dimension).label
        except Exception:
            label = f.dimension
        if label not in missed:
            missed.append(label)
    return f"not filtered by {', '.join(missed)}" if missed else ""


def insight_status(
    refs: Sequence[Any], facets: Sequence[Facet], found: Sequence[Insight]
) -> InsightStatus | None:
    """What a chart says about its rules: they found something, or nothing stands out, or they
    cannot say because no comparison is valid. None for a chart without rules or without sales."""
    if not refs:
        return None
    if found:
        return "found"
    live = [f.result.meta for f in facets if not f.result.meta.empty]
    if not live:
        return None
    # a chart that asks for a comparison and has none says so, even when a rule that needs no
    # comparison (a record) was looked at: silence there is not "nothing stands out"
    if any(m.has_comparison for m in live) and not any(m.compare_ok for m in live):
        return "no_comparison"
    return "quiet"


def run_slot(
    slot: BoundSlot,
    ctx: QueryContext,
    in_force: Sequence[AppliedFilter] = (),
    insights: str = "none",
) -> SlotRun:
    """Run a slot's component and its insight rules. `insights: chart` (the report's option) only
    decides whether the sentences are drawn; the rules run either way."""
    ref = slot.ref
    facets = run_facets(ref.component, ctx, ref.params, ref.with_)
    title, hint, note = slot_texts(ref, facets)
    unfiltered = unfiltered_note(ctx, slot, in_force)
    if unfiltered:
        hint = f"{hint} · {unfiltered}" if hint else unfiltered
    context = InsightContext(ctx.calendar)
    found = component_insights(ref.bound.id, ref.bound.spec.insights, facets, context)
    refs = list(ref.bound.spec.insights)
    seen = list(facets)
    for source in slot.sources:
        source_facets = run_facets(source.component, ctx, source.params, source.with_)
        found += component_insights(
            source.bound.id, source.bound.spec.insights, source_facets, context
        )
        refs += source.bound.spec.insights
        seen += source_facets
    status = insight_status(refs, seen, found)
    return SlotRun(
        slot,
        title,
        hint,
        note,
        facets,
        collect(found, CHART_MAX),
        found,
        status,
        insights == "chart",
    )


def run_page(page: ReportPage, ctx: QueryContext, insights: str = "none") -> PageRun:
    chips, note = page_chrome(ctx, page)
    slots = [run_slot(s, ctx, page.filters, insights) for s in page.slots]
    found = [i for s in slots for i in s.candidates]
    return PageRun(page, chips, note, slots, collect(found))


# --- the text blocks ------------------------------------------------------------------------------


def _component_lines(ref: BoundRef, facets: Sequence[Facet], theme: Theme) -> list[str]:
    visual = ref.bound.spec.visual
    lines: list[str] = []
    for facet in facets:
        ctx = DrawContext(theme, facet.metric, facet.label)
        lines += visuals.plain_text(visual.type, visual.options, facet.result, ctx)
    return lines or [visuals.EMPTY_TEXT]


def top_insights(pages: Sequence[PageRun], top: int, only: Sequence[str] | None) -> list[Insight]:
    """The best sentences across pages: ranked by score, the same sentence of two pages kept
    apart (a period's movers are not the next period's)."""
    found = []
    for run in pages:
        if only is not None and run.page.base not in only:
            continue
        found += [replace(i, key=(run.page.id, *i.key)) for i in run.insights]
    return collect(found, top)


def text_limit(report: Report, block: str) -> int:
    """The longest a block may be: the strictest limit of the deliveries that send it."""
    limits = [d.limit for d in report.spec.deliver.telegram if d.text == block]
    return min(limits) if limits else 1024


def check_length(report: Report, block: str, text: str) -> None:
    limit = text_limit(report, block)
    length = len(text.replace(DRIVE_PLACEHOLDER, "x" * DRIVE_URL_LENGTH))
    if length > limit:
        raise BuildError(
            f"{report.source}: text block {block} has {length} characters; Telegram takes at "
            f"most {limit} there (a Drive link counts as {DRIVE_URL_LENGTH})"
        )


def render_text_blocks(
    report: Report,
    anchor: date,
    pages: Sequence[PageRun],
    refs: Mapping[tuple[str, int], tuple[BoundRef, list[Facet]]],
    theme: Theme,
) -> dict[str, str]:
    """The Telegram text of every block in `outputs.telegram`, with `{{ drive_url }}` left in
    for the deliver step."""
    names = {**context(report, anchor, report.params), "drive_url": DRIVE_PLACEHOLDER}
    texts: dict[str, str] = {}
    for block in report.spec.outputs.telegram:
        parts: list[str] = []
        for i, item in enumerate(report.spec.text_blocks[block], start=1):
            where = f"{report.source}: text_blocks.{block}.{i}"
            if isinstance(item, TextLine):
                parts.append(render(item.text, names, where))
            elif isinstance(item, InsightsItem):
                spec = item.insights
                sentences = top_insights(pages, spec.top, spec.pages)
                if sentences:
                    parts.append("\n".join(f"• {s.text}" for s in sentences))
            else:
                ref, facets = refs[(block, i)]
                parts.append("\n".join(_component_lines(ref, facets, theme)))
        text = "\n\n".join(p for p in parts if p)
        check_length(report, block, text)
        texts[block] = text
    return texts


# --- the build ------------------------------------------------------------------------------------


def run_report(
    report: Report,
    ctx: QueryContext,
    generated: datetime | None = None,
    theme: Theme = DEFAULT_THEME,
) -> ReportRun:
    """Run a report on the models of a run: its pages, text blocks and sheets, as data."""
    anchor = ctx.calendar.anchor
    pages = [
        run_page(p, ctx, report.spec.insights) for p in expand(report, anchor, DataResolver(ctx))
    ]
    if not pages:
        raise BuildError(f"{report.source}: the report has no page (every repeat is empty)")
    bound = bind_refs(report)
    text_refs: dict[tuple[str, int], tuple[BoundRef, list[Facet]]] = {}
    sheets: list[SheetRun] = []
    for where, kind, ref in bound:
        facets = run_facets(ref.component, ctx, ref.params, ref.with_)
        if kind == "xlsx":
            sheets.append(SheetRun(ref, facets))
        else:
            _, block, index = where.split(".")
            text_refs[(block, int(index))] = (ref, facets)
    texts = render_text_blocks(report, anchor, pages, text_refs, theme)
    return ReportRun(
        report, anchor, generated or project().now().replace(tzinfo=None), pages, texts, sheets, ctx
    )


@dataclass(frozen=True)
class BuildResult:
    run: ReportRun
    out_dir: Path
    pdf: Path | None
    pngs: dict[str, Path]  # by page id
    xlsx: Path | None
    bundle: Path
    texts: dict[str, Path]
    overflows: list[Overflow]

    @property
    def files(self) -> list[Path]:
        found = [self.pdf, self.xlsx, self.bundle, *self.pngs.values(), *self.texts.values()]
        return [p for p in found if p is not None]


def build_report(
    report: Report,
    ctx: QueryContext,
    out_dir: Path,
    generated: datetime | None = None,
    theme: Theme = DEFAULT_THEME,
) -> BuildResult:
    """Run a report and write its outputs to `out_dir`."""
    from hari_lari_analytics.reporting.outputs import write_outputs

    run = run_report(report, ctx, generated, theme)
    return write_outputs(run, out_dir, theme)
