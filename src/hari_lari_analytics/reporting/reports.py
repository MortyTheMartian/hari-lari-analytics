"""Reports: the YAML schema, the loader, and the expansion of a report into bound pages.

A report is one YAML file in `reports/` (docs/components.md, "Reports"): parameters, filters,
a schedule, the outputs, the text blocks of the Telegram messages, the delivery and the pages,
each page a grid of slots that name components. Loading validates it strictly, like a
component: an unknown key fails with the file and the field, and every page is bound to its
components without touching data, so a wrong parameter, a filter that fits nothing, a slot
that is too small or two slots that overlap fail at once (`hari-lari-report validate`).

`expand` turns a report into its pages: repeats become one page per value (a list of periods,
the top 5 categories, decided against the data by the builder), every slot gets its component
bound with the parameters of the report, the repeat, the page and the slot, and the filters of
the report and the page merged in. The builder (`builder.py`) runs the result.
"""

from __future__ import annotations

import json
import re
import zoneinfo
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

import jinja2
import jinja2.meta
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from hari_lari_analytics.project import project
from hari_lari_analytics.reporting import insights as insights_module
from hari_lari_analytics.reporting import layouts, visuals
from hari_lari_analytics.reporting.components import (
    NAME,
    BoundComponent,
    Component,
    ComponentError,
    deep_merge,
)
from hari_lari_analytics.reporting.layouts import GridSpec, LayoutError, Rect, SlotSpec
from hari_lari_analytics.reporting.registry import Registry, RegistryError, default_registry
from hari_lari_analytics.reporting.windows import parse_window

SCHEMA_REF = "# yaml-language-server: $schema=../schema/report.schema.json"
TELEGRAM_CAPTION_LIMIT = 1024  # a caption under a photo or document
TELEGRAM_MESSAGE_LIMIT = 4096
DRIVE_PLACEHOLDER = "{{ drive_url }}"  # in a text block until the deliver step knows the link
DRIVE_URL_LENGTH = 100  # what a Drive link counts as when a text block is measured
PLACEHOLDER = "§"  # the value a repeat over `top` gets in a check without data
REPORT_VARIABLES = ("anchor", "anchor_iso_week", "report")  # besides the parameters

ParamValue = bool | int | float | str | list[bool | int | float | str]
FilterValue = bool | int | float | str | None | list[bool | int | float | str | None]


class ReportError(ValueError):
    """A report is invalid; the message names the file and the field."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# --- the schema -----------------------------------------------------------------------------------


class FilterSpec(_Strict):
    """Filters of a report or a page, with the meaning they have in a query: `where` narrows the
    universe, `focus` picks the members (docs/components.md, "Filters")."""

    where: dict[str, FilterValue] = {}
    focus: dict[str, FilterValue] = {}


class ScheduleSpec(_Strict):
    cron: str
    timezone: str | None = None
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, cron: str) -> str:
        problem = cron_problem(cron)
        if problem:
            raise ValueError(problem)
        return cron

    @field_validator("timezone")
    @classmethod
    def _zone(cls, name: str | None) -> str | None:
        if name is not None:
            try:
                zoneinfo.ZoneInfo(name)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"{name!r} is not a time zone") from None
        return name


class ComponentRef(_Strict):
    """A component outside a page grid (a text block, a sheet of the workbook): a slot without
    a position."""

    component: str
    with_: dict | None = Field(default=None, alias="with")
    params: dict = {}
    ignore_filters: bool | list[str] = False


class OutputsSpec(_Strict):
    pdf: bool = False
    png: list[str] | Literal["all"] = []  # page ids (a repeated page's base id covers them all)
    telegram: list[str] = []  # the text blocks to render
    xlsx: list[str | ComponentRef] = []  # the components, one sheet each


class TextLine(_Strict):
    """A template line; `drive_url` stays in it until the deliver step."""

    text: str


class InsightsLine(_Strict):
    """The best insight sentences of the pages, ranked, one line each."""

    top: int = Field(default=3, ge=1)
    pages: list[str] | None = None  # base ids of pages; default: every page


class InsightsItem(_Strict):
    insights: InsightsLine


def _text_kind(value: Any) -> str:
    if isinstance(value, Mapping):
        if "text" in value:
            return "text"
        if "insights" in value:
            return "insights"
    return "component"


TextItem = Annotated[
    Annotated[TextLine, Tag("text")]
    | Annotated[InsightsItem, Tag("insights")]
    | Annotated[ComponentRef, Tag("component")],
    Discriminator(_text_kind),
]


class DriveSpec(_Strict):
    folder: str
    files: list[Literal["pdf", "xlsx", "json"]] = Field(min_length=1)


class TelegramDelivery(_Strict):
    to: str
    photo: str | None = None  # a page of `outputs.png`; the text is its caption
    document: Literal["pdf", "xlsx"] | None = None
    text: str | None = None  # a text block of `outputs.telegram`

    @model_validator(mode="after")
    def _something(self) -> TelegramDelivery:
        if not (self.photo or self.document or self.text):
            raise ValueError("send a photo, a document or a text")
        return self

    @property
    def limit(self) -> int:
        """The characters the text may have: a caption under a photo or document, else a message."""
        return TELEGRAM_CAPTION_LIMIT if self.photo or self.document else TELEGRAM_MESSAGE_LIMIT


class Condition(_Strict):
    """When to deliver: the rule wrote a sentence, the component has rows, a data check warned."""

    insight: str | None = None
    rows: str | None = None
    checks: Literal["warn"] | None = None

    @model_validator(mode="after")
    def _one(self) -> Condition:
        given = [k for k in ("insight", "rows", "checks") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("a condition is exactly one of insight, rows and checks")
        return self


class DeliverSpec(_Strict):
    when: list[Condition] = []
    drive: DriveSpec | None = None
    telegram: list[TelegramDelivery] = []


class TopRepeat(_Strict):
    """The top members of a dimension, decided over a window under the `where` of the report."""

    dimension: str
    top: int = Field(ge=1)
    by: str  # a metric
    window: str  # a window name, or a parameter such as $period
    model: str | None = None  # default: the first exposed model that has both


RepeatValues = list[ParamValue] | TopRepeat


class WhenEmpty(_Strict):
    """What replaces a repeated group whose window has no sales: one page with a message."""

    title: str = "Overview"
    message: str = "No sales in this period"


class PageSpec(_Strict):
    id: str = Field(pattern=NAME)
    title: str  # a template: the parameters, `anchor` and `anchor_iso_week` are available
    params: dict[str, ParamValue] = {}
    filters: FilterSpec = FilterSpec()
    grid: GridSpec
    slots: list[SlotSpec] = Field(min_length=1)


def _one_parameter(repeat: dict) -> dict:
    if len(repeat) != 1:
        raise ValueError("repeat exactly one parameter")
    (name,) = repeat
    if not re.fullmatch(NAME, name):
        raise ValueError(f"{name}: a parameter name is snake_case")
    return repeat


class RepeatSpec(_Strict):
    """Pages repeated over the values of one parameter."""

    repeat: dict[str, RepeatValues]
    when_empty: WhenEmpty | None = None
    pages: list[PageSpec] = Field(min_length=1)

    @field_validator("repeat")
    @classmethod
    def _one(cls, repeat: dict) -> dict:
        return _one_parameter(repeat)


def _page_kind(value: Any) -> str:
    return "repeat" if isinstance(value, Mapping) and "repeat" in value else "page"


PageItem = Annotated[
    Annotated[PageSpec, Tag("page")] | Annotated[RepeatSpec, Tag("repeat")],
    Discriminator(_page_kind),
]


class ReportSpec(_Strict):
    id: str = Field(pattern=NAME)
    title: str
    description: str | None = None
    kicker: str | None = None  # the line above a page title; default: the report's title
    footer: str | None = None  # the start of the page footer; default: the kicker
    params: dict[str, ParamValue] = {}
    filters: FilterSpec = FilterSpec()
    schedule: ScheduleSpec | None = None
    outputs: OutputsSpec = OutputsSpec()
    text_blocks: dict[str, list[TextItem]] = {}
    deliver: DeliverSpec = DeliverSpec()
    repeat: dict[str, RepeatValues] | None = None  # every page, repeated
    pages: list[PageItem] = Field(min_length=1)

    @field_validator("text_blocks")
    @classmethod
    def _blocks(cls, blocks: dict) -> dict:
        for name, items in blocks.items():
            if not re.fullmatch(NAME, name):
                raise ValueError(f"{name}: a text block name is snake_case")
            if not items:
                raise ValueError(f"{name}: a text block needs at least one item")
        return blocks

    @field_validator("repeat")
    @classmethod
    def _one(cls, repeat: dict | None) -> dict | None:
        return None if repeat is None else _one_parameter(repeat)

    def plain_pages(self) -> list[PageSpec]:
        """Every page definition, inside a repeat or not."""
        return [
            p
            for item in self.pages
            for p in (item.pages if isinstance(item, RepeatSpec) else [item])
        ]


# --- schedules ------------------------------------------------------------------------------------

_CRON_FIELDS = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    (
        "month",
        1,
        12,
        {n: i for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)},
    ),
    ("day of week", 0, 7, {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}),
]


def cron_problem(cron: str) -> str | None:
    """Why a five-field cron expression is wrong, or None."""
    fields = cron.split()
    if len(fields) != 5:
        return f"{cron!r}: five fields (minute hour day month weekday), not {len(fields)}"
    for text, (label, low, high, names) in zip(fields, _CRON_FIELDS, strict=True):

        def number(token: str, names=names) -> int:
            if token.lower() in names:
                return names[token.lower()]
            if not token.isdigit():
                raise ValueError(token)
            return int(token)

        for part in text.split(","):
            body, _, step = part.partition("/")
            try:
                if step and (not step.isdigit() or int(step) < 1):
                    raise ValueError(step)
                if body == "*":
                    continue
                first, _, last = body.partition("-")
                values = [number(first)] + ([number(last)] if last else [])
            except ValueError:
                return f"{cron!r}: {label}: {part!r} is not valid"
            if any(not low <= v <= high for v in values) or (last and values[0] > values[1]):
                return f"{cron!r}: {label}: {part!r} is outside {low}-{high}"
    return None


# --- loaded reports -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    """A validated report: its spec, the file it came from, and the components it can use."""

    spec: ReportSpec
    source: str
    components: Mapping[str, Component]
    registry: Registry

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def kicker(self) -> str:
        return self.spec.kicker or self.spec.title

    @property
    def footer(self) -> str:
        return self.spec.footer or self.kicker

    @property
    def params(self) -> dict[str, Any]:
        """The parameters components get from the report (not the anchor, which the data
        decides)."""
        return {k: v for k, v in self.spec.params.items() if k != "anchor"}


_TAGS = frozenset({"page", "repeat", "text", "insights", "component"})


def _location(loc: Iterable) -> str:
    """A pydantic location without the tags of the unions of pages and text items."""
    parts: list[str] = []
    previous: Any = None
    for part in loc:
        if not (isinstance(previous, int) and part in _TAGS):
            parts.append(str(part))
        previous = part
    return ".".join(parts) or "report"


def _join(source: str, messages: Sequence[str]) -> str:
    if len(messages) == 1:
        return f"{source}: {messages[0]}"
    return f"{source}:\n" + "\n".join(f"  - {m}" for m in messages)


def parse_report(
    raw: Any,
    source: str,
    components: Mapping[str, Component],
    registry: Registry | None = None,
) -> Report:
    """A report from its YAML mapping, checked against the components and the registries."""
    registry = registry or default_registry()
    if not isinstance(raw, Mapping):
        raise ReportError(f"{source}: expected a mapping with id, title, pages, ...")
    try:
        spec = ReportSpec.model_validate(raw)
    except ValidationError as error:
        messages = [
            f"{_location(e['loc'])}: {e['msg'].removeprefix('Value error, ')}"
            for e in error.errors()
        ]
        raise ReportError(_join(source, messages)) from None
    report = Report(spec, source, components, registry)
    problems = check_report(report)
    if problems:
        raise ReportError(_join(source, problems))
    return report


def load_report(
    path: Path, components: Mapping[str, Component], registry: Registry | None = None
) -> Report:
    """Load one report file; its id must be its file name."""
    source = f"reports/{path.name}"
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ReportError(f"{source}: not valid YAML: {error}") from None
    report = parse_report(raw, source, components, registry)
    if report.id != path.stem:
        raise ReportError(f"{source}: id: {report.id} must be the file name ({path.stem})")
    return report


def reports_dir() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "reports"))


def load_reports(
    components: Mapping[str, Component],
    directory: Path | None = None,
    registry: Registry | None = None,
) -> dict[str, Report]:
    """Every report of a folder (default: the packaged `reports/`), by id."""
    directory = directory or reports_dir()
    if not directory.is_dir():
        return {}
    paths = sorted(directory.glob("*.yml"))
    return {r.id: r for r in (load_report(p, components, registry) for p in paths)}


# --- bound pages ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AppliedFilter:
    kind: Literal["where", "focus"]
    dimension: str
    origin: Literal["report", "page"]
    value: Any


@dataclass(frozen=True)
class BoundRef:
    """A component bound to the parameters and filters it runs with: a slot's, or a text block's
    or a sheet's."""

    component: Component
    params: dict[str, Any]  # what `run_facets` binds with
    with_: dict | None  # the overrides: the filters merged with the `with` of the reference
    bound: BoundComponent
    filters: tuple[AppliedFilter, ...]
    where: str  # for messages


@dataclass(frozen=True)
class BoundSlot:
    """A slot with its component bound and its rectangle."""

    spec: SlotSpec
    rect: Rect
    ref: BoundRef
    visual: str
    card: bool  # draw a card with the title and the hint

    @property
    def bound(self) -> BoundComponent:
        return self.ref.bound


@dataclass(frozen=True)
class ReportPage:
    id: str
    title: str
    params: dict[str, Any]
    period: str | None  # the page's window (its `period` parameter), for the header chips
    filters: tuple[AppliedFilter, ...]  # in force on the page, for the header chips
    grid: GridSpec
    slots: list[BoundSlot]
    base: str  # the id before the suffix of a repeat
    empty_message: str | None = None  # set on the page that replaces an empty repeated group


class Resolver:
    """Decides what depends on the data when a report is expanded: the values of a repeat and
    whether a window has sales. This one needs no data: a repeat over `top` gets one
    placeholder and nothing is empty (the check of `validate`)."""

    def values(
        self, name: str, spec: RepeatValues, params: Mapping[str, Any], report: Report
    ) -> list[Any]:
        if isinstance(spec, TopRepeat):
            return [f"{PLACEHOLDER}{name}"]
        return list(spec)

    def is_empty(self, window: str, params: Mapping[str, Any]) -> bool:
        return False


def slug(value: Any) -> str:
    """A value as part of a page id: `L6W` is `l6w`, `Coffee & Tea` is `coffee_tea`."""
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "x"


_TEMPLATES = jinja2.Environment(undefined=jinja2.StrictUndefined)


def context(report: Report, anchor: date, params: Mapping[str, Any]) -> dict[str, Any]:
    """The names a title, a folder or a text line may use."""
    iso = anchor.isocalendar()
    return {
        **params,
        "anchor": anchor,
        "anchor_iso_week": f"{iso.year}-W{iso.week:02d}",
        "report": report.spec.title,
    }


def render(template: str, names: Mapping[str, Any], where: str) -> str:
    """A template of a title, a folder name or a text line; an undefined name is an error."""
    try:
        return _TEMPLATES.from_string(template).render(**names)
    except jinja2.TemplateError as error:
        raise ReportError(f"{where}: {error}") from None


def template_names(template: str) -> set[str]:
    return set(jinja2.meta.find_undeclared_variables(_TEMPLATES.parse(template)))


def _ignored(ignore: bool | list[str], dimension: str) -> bool:
    return ignore is True or (isinstance(ignore, list) and dimension in ignore)


def merge_filters(
    report: Report,
    component: Component,
    with_: Mapping | None,
    ignore: bool | list[str],
    page_filters: FilterSpec | None,
    where: str,
) -> tuple[dict | None, tuple[AppliedFilter, ...]]:
    """The overrides of a component with the filters of the report and the page merged in.

    A filter applies when the component queries a model that has its dimension and the slot
    does not opt out. It replaces the component's own filter of the same kind on the same
    dimension; the page's replaces the report's, and the slot's own `with` replaces both."""
    merged = deep_merge(component.raw, with_ or {})
    query = merged.get("query")
    model = query.get("model") if isinstance(query, Mapping) else None
    own = (with_ or {}).get("query") or {}
    applied: dict[tuple[str, str], AppliedFilter] = {}
    if model is not None and ignore is not True:
        for origin, spec in (("report", report.spec.filters), ("page", page_filters)):
            if spec is None:
                continue
            for kind in ("where", "focus"):
                for dimension, value in getattr(spec, kind).items():
                    if _ignored(ignore, dimension):
                        continue
                    try:
                        report.registry.dimension(dimension).column_on(model)
                    except RegistryError:
                        continue
                    applied[(kind, dimension)] = AppliedFilter(kind, dimension, origin, value)
    facet = merged.get("facet")
    facet_by = facet.get("by") if isinstance(facet, Mapping) else None
    for f in applied.values():
        if f.dimension == facet_by:
            raise ReportError(
                f"{where}: the {f.origin}'s filter on {f.dimension} would be replaced by the "
                f"facet by {f.dimension} of {component.id}: add ignore_filters: "
                f"[{f.dimension}] to the slot"
            )
    overrides: dict[str, dict] = {"query": {}}
    kept: list[AppliedFilter] = []
    for (kind, dimension), f in applied.items():
        if dimension in (own.get(kind) or {}):
            continue  # the slot's own `with` wins
        overrides["query"].setdefault(kind, {})[dimension] = f.value
        kept.append(f)
    if not kept:
        return (dict(with_) if with_ else None), ()
    return deep_merge(overrides, with_ or {}), tuple(kept)


def bind_ref(
    report: Report,
    ref: SlotSpec | ComponentRef,
    scope: Mapping[str, Any],
    page_filters: FilterSpec | None,
    where: str,
) -> BoundRef:
    """Bind the component of a slot or a text block with the parameters in scope, the
    reference's own `params` on top, and the filters in force."""
    if ref.component not in report.components:
        known = ", ".join(sorted(report.components)) or "none"
        raise ReportError(f"{where}: no component {ref.component!r} (known: {known})")
    component = report.components[ref.component]
    with_, applied = merge_filters(
        report, component, ref.with_, ref.ignore_filters, page_filters, where
    )
    params = {**scope, **ref.params}
    try:
        bound = component.bind(params, with_)
    except ComponentError as error:
        note = ""
        if applied:
            filters = ", ".join(f"{f.kind} {f.dimension} of the {f.origin}" for f in applied)
            note = f" (filters in force: {filters})"
        raise ReportError(f"{where}: {error}{note}") from None
    return BoundRef(component, params, with_, bound, applied, where)


def facet_count(bound: BoundComponent) -> int | None:
    """How many facets a slot has, when that is known without data."""
    facet = bound.spec.facet
    if facet is None:
        return 1
    values = facet.values
    if isinstance(values, list):
        return len(values)
    if facet.by == "metric":
        return len(getattr(bound.query, "metrics", [])) or None
    return None if isinstance(values, str) else values.top


def draws_own_card(visual: str) -> bool:
    """A visual with a `card` option draws its own card (the KPI cards)."""
    return "card" in visuals.get(visual).options.model_fields


def _bind_slot(
    report: Report,
    placement: layouts.Placement,
    scope: Mapping[str, Any],
    page_filters: FilterSpec,
    where: str,
) -> BoundSlot:
    ref = bind_ref(report, placement.slot, scope, page_filters, where)
    visual_spec = ref.bound.spec.visual
    if visual_spec is None:
        raise ReportError(f"{where}: the component {ref.bound.id} has no visual to draw")
    card = placement.slot.card
    if card is None:
        card = not draws_own_card(visual_spec.type)
    try:
        layouts.check_size(
            placement.rect,
            visual_spec.type,
            card,
            bool(ref.bound.spec.hint),
            layouts.facet_cells(ref.bound.spec.facet, facet_count(ref.bound)),
            where,
        )
    except LayoutError as error:
        raise ReportError(str(error)) from None
    return BoundSlot(placement.slot, placement.rect, ref, visual_spec.type, card)


def filters_in_force(report: Report, page: PageSpec | None) -> tuple[AppliedFilter, ...]:
    """The filters of the report and the page, for the chips of a page header."""
    found = []
    for origin, spec in (("report", report.spec.filters), ("page", page.filters if page else None)):
        if spec is None:
            continue
        for kind in ("where", "focus"):
            found += [
                AppliedFilter(kind, dimension, origin, value)
                for dimension, value in getattr(spec, kind).items()
            ]
    return tuple(found)


def _bind_page(
    report: Report, page: PageSpec, scope: Mapping[str, Any], suffix: str, anchor: date
) -> ReportPage:
    where_page = f"{report.source}: page {page.id}{suffix}"
    params = {**scope, **page.params}
    title = render(page.title, context(report, anchor, params), f"{where_page}: title")
    try:
        placed = layouts.place(page.grid, page.slots, where_page)
    except LayoutError as error:
        raise ReportError(str(error)) from None
    slots = [
        _bind_slot(report, p, params, page.filters, f"{where_page}: slot {i} ({p.slot.component})")
        for i, p in enumerate(placed, start=1)
    ]
    return ReportPage(
        id=page.id + suffix,
        title=title,
        params=params,
        period=str(params["period"]) if "period" in params else None,
        filters=filters_in_force(report, page),
        grid=page.grid,
        slots=slots,
        base=page.id,
    )


def _empty_page(
    report: Report, empty: WhenEmpty, scope: Mapping[str, Any], suffix: str, anchor: date
) -> ReportPage:
    names = context(report, anchor, scope)
    return ReportPage(
        id=f"empty{suffix}",
        title=render(empty.title, names, f"{report.source}: when_empty.title"),
        params=dict(scope),
        period=str(scope["period"]) if "period" in scope else None,
        filters=(),
        grid=GridSpec(rows=[1]),
        slots=[],
        base="empty",
        empty_message=render(empty.message, names, f"{report.source}: when_empty.message"),
    )


def expand(
    report: Report,
    anchor: date,
    resolver: Resolver | None = None,
    only: PageItem | None = None,
) -> list[ReportPage]:
    """The pages of a report: repeats expanded, every slot bound. `only` expands one entry of
    `pages` instead of all. Without a resolver, a repeat over `top` gets a placeholder (the
    check of `validate`)."""
    resolver = resolver or Resolver()
    pages: list[ReportPage] = []

    def walk(items: Sequence, scope: Mapping[str, Any], suffix: str) -> None:
        for item in items:
            if isinstance(item, PageSpec):
                pages.append(_bind_page(report, item, scope, suffix, anchor))
                continue
            ((name, values_spec),) = item.repeat.items()
            for value in resolver.values(name, values_spec, scope, report):
                inner = {**scope, name: value}
                tail = f"{suffix}_{slug(value)}"
                if (
                    item.when_empty is not None
                    and name == "period"
                    and resolver.is_empty(str(value), inner)
                ):
                    pages.append(_empty_page(report, item.when_empty, inner, tail, anchor))
                else:
                    walk(item.pages, inner, tail)

    items: Sequence = report.spec.pages if only is None else [only]
    if report.spec.repeat:
        if any(not isinstance(i, PageSpec) for i in items):
            raise ReportError(
                f"{report.source}: with `repeat` at the top, list plain pages, not repeats"
            )
        items = [RepeatSpec(repeat=report.spec.repeat, pages=list(items))]
    walk(items, report.params, "")
    ids = [p.id for p in pages]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ReportError(f"{report.source}: page id(s) used twice: {', '.join(duplicates)}")
    return pages


# --- text blocks and sheets -----------------------------------------------------------------------


def component_refs(spec: ReportSpec) -> list[tuple[str, str, ComponentRef]]:
    """The components outside the pages: (where, kind, reference), kind `text` or `xlsx`."""
    refs: list[tuple[str, str, ComponentRef]] = []
    for name, items in spec.text_blocks.items():
        for i, item in enumerate(items, start=1):
            if isinstance(item, ComponentRef):
                refs.append((f"text_blocks.{name}.{i}", "text", item))
    for i, entry in enumerate(spec.outputs.xlsx, start=1):
        ref = entry if isinstance(entry, ComponentRef) else ComponentRef(component=entry)
        refs.append((f"outputs.xlsx.{i}", "xlsx", ref))
    return refs


def bind_refs(report: Report) -> list[tuple[str, str, BoundRef]]:
    """The components of the text blocks and the workbook, bound with the report's
    parameters and filters."""
    return [
        (where, kind, bind_ref(report, ref, report.params, None, f"{where} ({ref.component})"))
        for where, kind, ref in component_refs(report.spec)
    ]


# --- the check ------------------------------------------------------------------------------------


def top_model(registry: Registry, spec: TopRepeat) -> str:
    """The model a `top` repeat queries: the one named, else the first exposed model that has
    its dimension and its metric."""
    if spec.model:
        return spec.model
    for model in sorted(registry.models, key=lambda m: (m != "fct_sales_lines", m)):
        info = registry.models[model]
        if not (info.exposed and info.time):
            continue
        try:
            registry.dimension(spec.dimension).column_on(model)
            registry.aggregate(spec.by, model)
        except RegistryError:
            continue
        return model
    raise RegistryError(
        f"no model has both the dimension {spec.dimension} and the metric {spec.by}"
    )


def _repeat_problems(report: Report) -> list[str]:
    problems = []
    groups = [(r.repeat, r.when_empty) for r in report.spec.pages if isinstance(r, RepeatSpec)]
    if report.spec.repeat:
        groups.append((report.spec.repeat, None))
    for repeat, when_empty in groups:
        ((name, values),) = repeat.items()
        if isinstance(values, TopRepeat):
            try:
                if not values.window.startswith("$"):
                    parse_window(values.window)
                report.registry.dimension(values.dimension)
                report.registry.metric(values.by)
                top_model(report.registry, values)
            except (ValueError, RegistryError) as error:
                problems.append(f"repeat {name}: {error}")
        else:
            if not values:
                problems.append(f"repeat {name}: no values")
            if name == "period":
                for v in values:
                    try:
                        parse_window(str(v))
                    except ValueError as error:
                        problems.append(f"repeat period: {error}")
        if when_empty is not None and name != "period":
            problems.append(f"when_empty works for a repeat over period, not over {name}")
    return problems


def _filter_problems(report: Report) -> list[str]:
    problems = []
    specs = [("the report", report.spec.filters)] + [
        (f"page {p.id}", p.filters) for p in report.spec.plain_pages()
    ]
    for origin, spec in specs:
        for kind in ("where", "focus"):
            for dimension in getattr(spec, kind):
                try:
                    report.registry.dimension(dimension)
                except RegistryError:
                    problems.append(f"{origin}: filters.{kind}: unknown dimension {dimension}")
    return problems


def _output_problems(report: Report) -> list[str]:
    spec = report.spec
    problems = []
    targets = project().telegram_targets
    page_ids = {p.id for p in spec.plain_pages()}
    png = spec.outputs.png
    for page in [] if png == "all" else png:
        if page not in page_ids:
            problems.append(f"outputs.png: no page {page} (pages: {', '.join(sorted(page_ids))})")
    for name in spec.outputs.telegram:
        if name not in spec.text_blocks:
            problems.append(f"outputs.telegram: no text block {name}")
    allowed = {*REPORT_VARIABLES, "drive_url", *report.params}
    for name, items in spec.text_blocks.items():
        for item in items:
            if isinstance(item, TextLine):
                try:
                    unknown = template_names(item.text) - allowed
                except jinja2.TemplateSyntaxError as error:
                    problems.append(f"text_blocks.{name}: {error.message}")
                    continue
                if unknown:
                    problems.append(
                        f"text_blocks.{name}: {', '.join(sorted(unknown))} is not available "
                        f"({', '.join(sorted(allowed))})"
                    )
            elif isinstance(item, InsightsItem):
                for page in item.insights.pages or []:
                    if page not in page_ids:
                        problems.append(f"text_blocks.{name}: insights: no page {page}")
    problems += _delivery_problems(report, page_ids, targets)
    return problems


def _delivery_problems(report: Report, page_ids: set[str], targets: Mapping[str, str]) -> list[str]:
    spec = report.spec
    deliver = spec.deliver
    problems = []
    rules = insights_module.default_rules()
    for i, condition in enumerate(deliver.when, start=1):
        if condition.insight and condition.insight not in rules:
            problems.append(f"deliver.when.{i}: unknown insight rule {condition.insight}")
        if condition.rows and condition.rows not in report.components:
            problems.append(f"deliver.when.{i}: no component {condition.rows}")
    has = {"pdf": spec.outputs.pdf, "xlsx": bool(spec.outputs.xlsx), "json": True}
    if deliver.drive:
        names = {*REPORT_VARIABLES, *report.params}
        try:
            unknown = template_names(deliver.drive.folder) - names
        except jinja2.TemplateSyntaxError as error:
            problems.append(f"deliver.drive.folder: {error.message}")
        else:
            if unknown:
                problems.append(
                    f"deliver.drive.folder: {', '.join(sorted(unknown))} is not available "
                    f"({', '.join(sorted(names))})"
                )
        for f in deliver.drive.files:
            if not has[f]:
                problems.append(f"deliver.drive.files: {f} is not an output of the report")
    png = spec.outputs.png
    for i, entry in enumerate(deliver.telegram, start=1):
        where = f"deliver.telegram.{i}"
        if entry.to not in targets:
            problems.append(
                f"{where}: to: no target {entry.to!r} in project.yml "
                f"(telegram_targets: {', '.join(sorted(targets))})"
            )
        if entry.text is not None and entry.text not in spec.outputs.telegram:
            problems.append(f"{where}: text: {entry.text} is not listed in outputs.telegram")
        if entry.photo is not None:
            if entry.photo not in page_ids:
                problems.append(f"{where}: photo: no page {entry.photo}")
            elif png != "all" and entry.photo not in png:
                problems.append(f"{where}: photo: {entry.photo} is not listed in outputs.png")
        if entry.document is not None and not has[entry.document]:
            problems.append(f"{where}: document: {entry.document} is not an output of the report")
    return problems


def _ref_problems(report: Report) -> tuple[list[str], list[tuple[str, str, BoundRef]]]:
    problems, bound = [], []
    for where, kind, ref in component_refs(report.spec):
        label = f"{where} ({ref.component})"
        try:
            one = bind_ref(report, ref, report.params, None, label)
        except ReportError as error:
            problems.append(str(error))
            continue
        if kind == "text":
            visual = one.bound.spec.visual
            if visual is None or visuals.get(visual.type).text is None:
                what = (
                    "has no visual" if visual is None else f"visual {visual.type} has no text form"
                )
                problems.append(f"{label}: {what}: a text block needs a visual with a text form")
                continue
        bound.append((where, kind, one))
    return problems, bound


def _unused_filter_problems(
    report: Report, pages: list[ReportPage], refs: list[tuple[str, str, BoundRef]]
) -> list[str]:
    """A filter that applies to no slot of its report or page is an error: a typo, or a
    dimension no queried model has."""
    problems = []
    spec = report.spec
    for kind in ("where", "focus"):
        for dimension in getattr(spec.filters, kind):
            used = any(
                f.origin == "report" and (f.kind, f.dimension) == (kind, dimension)
                for slots in ([s.ref for p in pages for s in p.slots], [r for _, _, r in refs])
                for ref in slots
                for f in ref.filters
            )
            if not used:
                problems.append(
                    f"filters.{kind}.{dimension}: applies to no slot of the report (no component "
                    "queries a model that has the dimension, or every slot opts out)"
                )
    for page_spec in spec.plain_pages():
        mine = [p for p in pages if p.base == page_spec.id]
        for kind in ("where", "focus"):
            for dimension in getattr(page_spec.filters, kind):
                used = any(
                    f.origin == "page" and (f.kind, f.dimension) == (kind, dimension)
                    for page in mine
                    for slot in page.slots
                    for f in slot.ref.filters
                )
                if not used:
                    problems.append(
                        f"page {page_spec.id}: filters.{kind}.{dimension}: applies to no slot "
                        "of the page"
                    )
    return problems


def anchor_problem(value: Any) -> str | None:
    if value in ("yesterday", "today"):
        return None
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return f"params.anchor: {value!r} is not yesterday, today or a date (YYYY-MM-DD)"
    return None


def resolve_anchor(
    report: Report, override: date | None = None, now: datetime | None = None
) -> date:
    """The last day of a report: `--anchor`, else its `anchor` parameter (`yesterday`, `today`
    or a date), else the rule of project.yml."""
    if override is not None:
        return override
    value = report.spec.params.get("anchor")
    settings = project()
    if value is None:
        return settings.default_anchor(now)
    if value in ("yesterday", "today"):
        return settings.today(now) - timedelta(days=1 if value == "yesterday" else 0)
    return date.fromisoformat(str(value))


def check_report(report: Report) -> list[str]:
    """Every problem of a report that needs no data (an empty list when it is fine): the
    references, the filters, each page bound to its components, and the sizes of its slots."""
    problems = _repeat_problems(report) + _filter_problems(report) + _output_problems(report)
    if "anchor" in report.spec.params:
        problem = anchor_problem(report.spec.params["anchor"])
        problems += [problem] if problem else []
    if problems:
        return problems
    anchor = project().reference_date
    for item in report.spec.pages:
        try:
            expand(report, anchor, only=item)
        except ReportError as error:
            problems.append(str(error).removeprefix(f"{report.source}: "))
    ref_problems, refs = _ref_problems(report)
    problems += ref_problems
    if problems:
        return problems
    try:
        pages = expand(report, anchor)
    except ReportError as error:
        return [str(error).removeprefix(f"{report.source}: ")]
    return _unused_filter_problems(report, pages, refs)


# --- the JSON Schema ------------------------------------------------------------------------------


def report_json_schema(components: Iterable[str], registry: Registry | None = None) -> dict:
    """The JSON Schema of a report file: the component ids, the dimensions of the filters, the
    insight rules and the delivery targets, for autocompletion in the editor."""
    registry = registry or default_registry()
    schema = ReportSpec.model_json_schema()
    defs = schema["$defs"]
    dimensions = {
        "type": "string",
        "enum": sorted(registry.dimensions),
        "description": "a dimension of dimensions.yml",
    }
    metrics = {"type": "string", "enum": sorted(registry.metrics), "description": "a metric"}
    ids = {"type": "string", "enum": sorted(components), "description": "a component"}
    for kind in ("where", "focus"):
        defs["FilterSpec"]["properties"][kind]["propertyNames"] = dimensions
    defs["SlotSpec"]["properties"]["component"] = ids
    defs["ComponentRef"]["properties"]["component"] = ids
    defs["TopRepeat"]["properties"]["dimension"] = dimensions
    defs["TopRepeat"]["properties"]["by"] = metrics
    defs["Condition"]["properties"]["insight"] = {
        "anyOf": [
            {"type": "string", "enum": sorted(insights_module.default_rules())},
            {"type": "null"},
        ],
        "description": "a rule of insights.yml",
    }
    defs["Condition"]["properties"]["rows"] = {"anyOf": [ids, {"type": "null"}]}
    defs["TelegramDelivery"]["properties"]["to"] = {
        "type": "string",
        "enum": sorted(project().telegram_targets),
        "description": "a target of telegram_targets in project.yml",
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
        "title": "Hari Lari report",
    }


def schema_path() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "schema")) / (
        "report.schema.json"
    )


def schema_text(components: Iterable[str], registry: Registry | None = None) -> str:
    return json.dumps(report_json_schema(components, registry), indent=2) + "\n"


def write_schema(components: Iterable[str], path: Path | None = None) -> Path:
    path = path or schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_text(components))
    return path
