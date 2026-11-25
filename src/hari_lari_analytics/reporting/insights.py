"""The insight engine: rule types in Python, rules in `insights.yml`, scored sentences.

A rule type (`@rule_type("movers", ...)`) reads one result and returns findings: the variables
of a sentence as display strings (from `present.py`), a score, a key and a tone. A rule of
`insights.yml` picks a type, sets its options, a `weight` and the Jinja `text` of the sentence
(docs/components.md, "Insights"):

    movers:
      type: movers
      top: 2
      min_abs_delta: { pct: 0.05, idr: 2000 }
      text: "{{ row.label }}: {{ metric.label | lower }} {{ row.delta }} vs {{ compare.label }}"

The rules hold two promises: no sentence about a value below its metric's `min_n` (a small row,
or a change with a small side), and none about a change without a valid comparison (a rule
type that compares does not run then). A component lists its rules (`insights: [movers]`, or
`[{movers: {metrics: [penetration]}}]` to change options); `component_insights` evaluates them
on each facet and `collect` keeps the best sentence per key (the same metric and group), ranks
them by score and cuts to the top ones, for the `insights` visual and the plain text.

Scores are comparable across rules, about 0 to 1: the size of the change relative to its KPI
or total (a 5 pp move in a share scores 0.05), times the rule's `weight`.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import jinja2
import jinja2.meta
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hari_lari_analytics.reporting import formats
from hari_lari_analytics.reporting.compiler import Query
from hari_lari_analytics.reporting.formats import Tone
from hari_lari_analytics.reporting.present import Presentation, Row, present
from hari_lari_analytics.reporting.queries import SourceQuery
from hari_lari_analytics.reporting.registry import Format, Registry
from hari_lari_analytics.reporting.result import Result, Shape
from hari_lari_analytics.reporting.windows import Calendar


class InsightError(ValueError):
    """A rule or a component's use of it is invalid; the message names the file and the rule."""


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Finding:
    """What a rule type found: the sentence's variables, its score before the weight, the key
    that identifies what it speaks about, and its tone."""

    values: dict[str, Any]
    score: float
    key: tuple
    tone: Tone | None = None


@dataclass(frozen=True)
class Insight:
    text: str
    score: float
    rule: str
    key: tuple
    tone: Tone | None = None
    component: str | None = None
    facet: str | None = None  # the facet's label


@dataclass(frozen=True)
class InsightContext:
    calendar: Calendar


Fn = Callable[[Result, Any, InsightContext], list[Finding]]
Check = Callable[[Any, "Query | SourceQuery", "Shape | None", Registry], None]


@dataclass(frozen=True)
class RuleType:
    name: str
    fn: Fn
    options: type[BaseModel]
    shapes: tuple[Shape, ...]
    variables: frozenset[str]
    compares: bool  # needs a valid comparison: it does not run without one
    check: Check | None
    description: str


RULE_TYPES: dict[str, RuleType] = {}


def rule_type(
    name: str,
    options: type[BaseModel],
    shapes: Iterable[Shape],
    variables: Iterable[str],
    compares: bool = False,
    check: Check | None = None,
) -> Callable[[Fn], Fn]:
    """Register a rule type; its docstring is its description, `variables` what its sentence
    may use."""

    def register(fn: Fn) -> Fn:
        if name in RULE_TYPES:
            raise ValueError(f"the rule type {name} is registered twice")
        doc = (fn.__doc__ or "").strip().splitlines()
        RULE_TYPES[name] = RuleType(
            name,
            fn,
            options,
            tuple(shapes),
            frozenset(variables) | {"window"},
            compares,
            check,
            doc[0] if doc else "",
        )
        return fn

    return register


# --- helpers ---------------------------------------------------------------------------------


def _metrics_of(query: Query | SourceQuery) -> list[str] | None:
    return list(query.metrics) if isinstance(query, Query) else None


def _need_metrics(rule: str, option: str, metrics: Iterable[str | None], query) -> None:
    have = _metrics_of(query)
    if have is None:
        return
    missing = [m for m in metrics if m is not None and m not in have]
    if missing:
        raise InsightError(
            f"{rule}: {option}: {', '.join(missing)} is not a metric of the query "
            f"({', '.join(have)})"
        )


def _window(result: Result) -> dict[str, str]:
    w = result.meta.window
    if w is None:
        return {"label": "", "title": ""}
    return {"label": w.current.label, "title": w.current.title}


def _compare(result: Result) -> dict[str, str]:
    w = result.meta.window
    return {"label": (w.compare_label if w is not None else None) or ""}


def _metric(result: Result, metric_id: str) -> dict[str, str]:
    spec = result.meta.metrics[metric_id]
    return {"id": spec.id, "label": spec.label, "unit": formats.unit(spec.format)}


def _orders(row: Row) -> str:
    return row["n_orders"].text if "n_orders" in row.cells else ""


def _cap(score: float) -> float:
    return max(0.0, min(float(score), 1.0))


# --- drivers ---------------------------------------------------------------------------------


class DriversOptions(Options):
    top: int = Field(default=2, ge=1)  # the drivers named in the sentence
    min_share_of_change: float = Field(default=0.2, ge=0, le=1)


@rule_type(
    "drivers",
    DriversOptions,
    shapes=("decomposition",),
    variables=("kpi", "compare", "drivers"),
    compares=True,
)
def drivers(result: Result, options: DriversOptions, ctx: InsightContext) -> list[Finding]:
    """The largest drivers of a KPI's change, from `decompose`: one sentence per KPI."""
    pres = present(result)
    df = result.df
    kpi = next((r for r in pres.rows if r.kind == "total"), None)
    if kpi is None or kpi.small or kpi["change"].value in (None, 0):
        return []
    steps = [r for r in pres.rows if r.kind != "total" and df["level"].iloc[r.position] == 1]
    if any(df["status"].iloc[r.position] == "undefined" for r in steps):
        return []
    named = [
        r
        for r in steps
        if not r.small
        and not r["effect"].small
        and r["effect"].value not in (None, 0)
        and abs(r["share_of_change"].value or 0) >= options.min_share_of_change
    ]
    named.sort(key=lambda r: abs(r["effect"].value), reverse=True)
    if not named:
        return []
    spec = result.meta.decomposition
    kpi_metric = result.meta.metrics[spec.kpi]
    values = {
        "kpi": {
            "label": kpi_metric.label,
            "unit": formats.unit(kpi_metric.format),
            "value": kpi["current"].text,
            "prior": kpi["prior"].text,
            "change": kpi["change"].text,
            "delta": kpi["delta"].text,
        },
        "compare": _compare(result),
        "drivers": [
            {
                "label": r.label,
                "effect": r["effect"].text,
                "share": r["share_of_change"].text,
                "change": r["change"].text,
                "delta": r["delta"].text,
            }
            for r in named[: options.top]
        ],
        "window": _window(result),
    }
    return [Finding(values, _cap(abs(kpi["change"].value)), (spec.kpi,), kpi["change"].tone)]


# --- movers ----------------------------------------------------------------------------------


class MoversOptions(Options):
    metrics: list[str] | None = None  # default: every metric of the result with a comparison
    top: int = Field(default=2, ge=1)
    # the smallest move worth a sentence, by the metric's format: {pct: 0.05} is 5 pp
    min_abs_delta: dict[Format, float] = {}


def _movers_check(options: MoversOptions, query, shape, registry) -> None:
    _need_metrics("movers", "metrics", options.metrics or [], query)


@rule_type(
    "movers",
    MoversOptions,
    shapes=("rows",),
    variables=("row", "metric", "compare"),
    compares=True,
    check=_movers_check,
)
def movers(result: Result, options: MoversOptions, ctx: InsightContext) -> list[Finding]:
    """The groups of a breakdown that moved most against the comparison."""
    pres = present(result)
    total = pres.total
    metrics = options.metrics or [m for m in pres.metrics if f"{m}_delta" in pres.columns]
    found = []
    for m in metrics:
        spec = result.meta.metrics[m]
        if spec.format == "timestamp" or f"{m}_delta" not in pres.columns:
            continue
        base = total[m].value if total is not None else None
        for row in pres.rows:
            delta = row[f"{m}_delta"]
            if row.kind != "row" or row[m].small or delta.small or delta.value in (None, 0):
                continue
            if abs(delta.value) < options.min_abs_delta.get(spec.format, 0):
                continue
            if spec.format == "pct":
                score = abs(delta.value)
            elif base:
                score = abs(delta.value) / abs(base)
            else:
                score = abs(row[f"{m}_change"].value or 0)
            values = {
                "row": {
                    "label": row.label,
                    "value": row[m].text,
                    "prior": row[f"prior_{m}"].text,
                    "delta": delta.text,
                    "change": row[f"{m}_change"].text,
                    "orders": _orders(row),
                },
                "metric": _metric(result, m),
                "compare": _compare(result),
                "window": _window(result),
            }
            found.append(Finding(values, _cap(score), (m, row.label), delta.tone))
    found.sort(key=lambda f: f.score, reverse=True)
    return found[: options.top]


# --- threshold -------------------------------------------------------------------------------


class ThresholdOptions(Options):
    metric: str
    above: float | None = None  # fires when the value rises to or above this from below
    below: float | None = None  # fires when the value falls to or below this from above
    rows: bool = False  # also each group of a breakdown, not only the total

    @model_validator(mode="after")
    def _one_bound(self):
        if (self.above is None) == (self.below is None):
            raise ValueError("give exactly one of above and below")
        return self


def _threshold_check(options: ThresholdOptions, query, shape, registry) -> None:
    _need_metrics("threshold", "metric", [options.metric], query)


@rule_type(
    "threshold",
    ThresholdOptions,
    shapes=("single", "rows"),
    variables=("row", "metric", "value", "prior", "threshold", "direction", "compare"),
    compares=True,
    check=_threshold_check,
)
def threshold(result: Result, options: ThresholdOptions, ctx: InsightContext) -> list[Finding]:
    """A value that crossed a threshold since the comparison period."""
    m = options.metric
    if m not in result.df or f"prior_{m}" not in result.df:
        raise InsightError(f"threshold: the result has no {m} with its prior value")
    spec = result.meta.metrics[m]
    pres = present(result)
    rows = [r for r in pres.rows if r.kind == "row"] if options.rows else []
    if pres.total is not None:
        rows.append(pres.total)
    up = options.above is not None
    level = options.above if up else options.below
    found = []
    for row in rows:
        now, before = row[m], row[f"prior_{m}"]
        if now.small or before.small or now.value is None or before.value is None:
            continue
        crossed = before.value < level <= now.value if up else before.value > level >= now.value
        if not crossed:
            continue
        past = abs(now.value - level) / abs(level) if level else 0.0
        values = {
            "row": {"label": row.label, "orders": _orders(row)},
            "metric": _metric(result, m),
            "value": now.text,
            "prior": before.text,
            "threshold": formats.value_text(level, spec.format),
            "direction": "above" if up else "below",
            "compare": _compare(result),
            "window": _window(result),
        }
        tone = formats.tone(1 if up else -1, spec.up_is_good)
        found.append(Finding(values, _cap(0.5 + past / 2), (m, row.label, "threshold"), tone))
    return found


# --- record ----------------------------------------------------------------------------------


class RecordOptions(Options):
    metric: str | None = None  # default: the first metric of the result
    min_buckets: int = Field(default=4, ge=2)  # the history a record needs


def _record_check(options: RecordOptions, query, shape, registry) -> None:
    _need_metrics("record", "metric", [options.metric], query)


@rule_type(
    "record",
    RecordOptions,
    shapes=("series",),
    variables=("metric", "bucket", "value", "previous", "grain", "since"),
    check=_record_check,
)
def record(result: Result, options: RecordOptions, ctx: InsightContext) -> list[Finding]:
    """The latest bucket of a trend is the best one (D19): the highest, or the lowest when down
    is good. A partial last bucket and buckets without trading days do not count."""
    meta = result.meta
    m = options.metric or next(
        (c for c, s in meta.value_metrics.items() if s.format != "timestamp"), None
    )
    if m is None or m not in result.df or meta.grain is None or meta.window is None:
        return []
    spec = meta.metrics[m]
    pres = present(result)
    rows = _record_rows(result, pres)
    if rows and _bucket(result, rows[-1])[1] > meta.window.current.end:
        rows = rows[:-1]  # the bucket is not over yet
    rows = [r for r in rows if r[m].value is not None]
    if len(rows) < options.min_buckets:
        return []
    latest, history = rows[-1], rows[:-1]
    if latest[m].small:
        return []
    lowest = spec.up_is_good is False
    pick = min if lowest else max
    best = pick(history, key=lambda r: r[m].value)
    beats = latest[m].value < best[m].value if lowest else latest[m].value > best[m].value
    if not beats:
        return []
    first = _bucket(result, rows[0])[0]
    opening = ctx.calendar.first_sale is not None and first <= ctx.calendar.first_sale
    grain = formats.GRAIN_WORD[meta.grain]
    since = "since opening" if opening else f"in the last {len(rows)} {grain}s"
    margin = abs(latest[m].value - best[m].value) / abs(best[m].value) if best[m].value else 1
    values = {
        "metric": _metric(result, m),
        "bucket": latest.label,
        "value": latest[m].text,
        "previous": {"bucket": best.label, "value": best[m].text},
        "grain": grain,
        "since": since,
        "window": _window(result),
    }
    tone: Tone = "neutral" if spec.up_is_good is None else "good"
    return [Finding(values, _cap(0.3 + margin), (m, "record"), tone)]


def _record_rows(result: Result, pres: Presentation) -> list[Row]:
    """The buckets in time order, with those before the window, without the empty days."""
    df = result.df
    rows = [r for r in pres.rows if r.kind == "row"]
    if "trading_days" in df:
        rows = [r for r in rows if not pd.isna(df["trading_days"].iloc[r.position])]
        rows = [r for r in rows if int(df["trading_days"].iloc[r.position]) > 0]
    return rows


def _bucket(result: Result, row: Row):
    column = next(d for d, s in result.meta.dimensions.items() if s.domain == "calendar")
    start = pd.Timestamp(result.df[column].iloc[row.position]).date()
    ends = {b.start: b.end for b in result.meta.buckets}
    return start, ends.get(start, start)


# --- notes -----------------------------------------------------------------------------------


class NotesOptions(Options):
    kinds: list[str] = ["extraction", "check"]  # the kinds of note worth a sentence
    score: float = Field(default=0.5, ge=0, le=1)


@rule_type("notes", NotesOptions, shapes=("rows",), variables=("note", "kind"))
def notes(result: Result, options: NotesOptions, ctx: InsightContext) -> list[Finding]:
    """The data notes (source `notes`): extraction errors and warnings, failing checks."""
    df = result.df
    if not {"kind", "note"} <= set(df.columns):
        raise InsightError("notes: the result has no kind and note columns (use source: notes)")
    found = []
    for kind, note in zip(df["kind"], df["note"], strict=True):
        if kind in options.kinds:
            values = {"note": str(note), "kind": str(kind), "window": _window(result)}
            found.append(Finding(values, options.score, ("note", str(note))))
    return found


# --- the rules -------------------------------------------------------------------------------


def _join_effects(drivers: Sequence[Mapping]) -> str:
    """ "basket size +3,210 and items per order -1,104" from the drivers of a sentence."""
    parts = [f"{d['label'][:1].lower() + d['label'][1:]} {d['effect']}" for d in drivers]
    return _and_join(parts)


def _and_join(parts: Sequence[str]) -> str:
    parts = list(parts)
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
_ENV.filters["join_effects"] = _join_effects
_ENV.filters["and_join"] = _and_join


@dataclass(frozen=True)
class Rule:
    name: str
    type: RuleType
    options: dict[str, Any]  # the type's options as written in insights.yml
    text: str
    template: jinja2.Template = field(compare=False, repr=False)
    weight: float = 1.0

    def resolved(self, overrides: Mapping[str, Any] | None = None) -> tuple[BaseModel, float]:
        """The type's options with a component's overrides, and the weight."""
        raw = {**self.options, **(overrides or {})}
        weight = float(raw.pop("weight", self.weight))
        try:
            return self.type.options.model_validate(raw), weight
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'options'}: "
                f"{e['msg'].removeprefix('Value error, ')}"
                for e in error.errors()
            )
            raise InsightError(f"{self.name}: {details}") from None


def parse_rules(raw: Any, source: str = "insights.yml") -> dict[str, Rule]:
    """The rules of an insights.yml mapping, each checked: its type, options, weight and the
    variables its text uses."""
    if not isinstance(raw, Mapping):
        raise InsightError(f"{source}: expected a mapping of rule names to rules")
    rules = {}
    for name, body in raw.items():
        where = f"{source}: {name}"
        if not isinstance(body, Mapping):
            raise InsightError(f"{where}: expected a mapping with type and text")
        body = dict(body)
        kind, text = body.pop("type", None), body.pop("text", None)
        if kind not in RULE_TYPES:
            known = ", ".join(sorted(RULE_TYPES))
            raise InsightError(f"{where}: type: unknown rule type {kind!r} ({known})")
        if not isinstance(text, str) or not text.strip():
            raise InsightError(f"{where}: text: the sentence is missing")
        weight = body.pop("weight", 1.0)
        if not isinstance(weight, int | float) or weight <= 0:
            raise InsightError(f"{where}: weight: a positive number")
        spec = RULE_TYPES[kind]
        try:
            template = _ENV.from_string(text)
            used = jinja2.meta.find_undeclared_variables(_ENV.parse(text))
        except jinja2.TemplateSyntaxError as error:
            raise InsightError(f"{where}: text: {error.message}") from None
        unknown = sorted(used - spec.variables)
        if unknown:
            raise InsightError(
                f"{where}: text: {', '.join(unknown)} is not a variable of a {kind} sentence "
                f"({', '.join(sorted(spec.variables))})"
            )
        rule = Rule(str(name), spec, body, text, template, float(weight))
        try:
            rule.resolved()
        except InsightError as error:
            raise InsightError(f"{source}: {error}") from None
        rules[rule.name] = rule
    return rules


def rules_path() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "insights.yml"))


def load_rules(path: Path | None = None) -> dict[str, Rule]:
    path = path or rules_path()
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise InsightError(f"{path.name}: not valid YAML: {error}") from None
    return parse_rules(raw, path.name)


@functools.cache
def default_rules() -> dict[str, Rule]:
    return load_rules()


# --- a component's rules ---------------------------------------------------------------------


def parse_ref(raw: Any) -> tuple[str, dict[str, Any]]:
    """A component's entry: `movers` or `{movers: {metrics: [penetration]}}`."""
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, Mapping) and len(raw) == 1:
        (name, overrides), *_ = raw.items()
        if overrides is None or isinstance(overrides, Mapping):
            return str(name), dict(overrides or {})
    raise InsightError("a rule name, or a mapping of one rule name to its options")


def check_insights(
    refs: Sequence[Any],
    query: Query | SourceQuery,
    shape: Shape | None,
    registry: Registry,
    rules: Mapping[str, Rule] | None = None,
) -> None:
    """Check a component's rules without data: known, their options, the result shape, the
    comparison a comparing rule needs and the metrics it names."""
    rules = default_rules() if rules is None else rules
    for i, raw in enumerate(refs):
        try:
            name, overrides = parse_ref(raw)
        except InsightError as error:
            raise InsightError(f"{i}: {error}") from None
        if name not in rules:
            known = ", ".join(sorted(rules)) or "none"
            raise InsightError(f"{i}: unknown rule {name!r} of insights.yml (known: {known})")
        rule = rules[name]
        options, _ = rule.resolved(overrides)
        if shape is not None and shape not in rule.type.shapes:
            raise InsightError(
                f"{name}: a {rule.type.name} rule reads a result of shape "
                f"{', '.join(rule.type.shapes)}, not {shape}"
            )
        if rule.type.compares and isinstance(query, Query) and query.compare == "none":
            raise InsightError(f"{name}: needs a comparison: set `compare` on the query")
        if rule.type.check is not None:
            rule.type.check(options, query, shape, registry)


def _sentence(text: str) -> str:
    """One line with single spaces, starting with a capital."""
    text = " ".join(text.split())
    return text[:1].upper() + text[1:]


def evaluate(
    rule: Rule,
    result: Result,
    ctx: InsightContext,
    overrides: Mapping[str, Any] | None = None,
    component: str | None = None,
    facet: str | None = None,
) -> list[Insight]:
    """The sentences of one rule on one result; none for an empty window or, for a comparing
    rule, without a valid comparison."""
    if result.meta.empty:
        return []
    if rule.type.compares and not result.meta.compare_ok:
        return []
    if result.meta.shape not in rule.type.shapes:
        raise InsightError(
            f"{rule.name}: a {rule.type.name} rule reads a result of shape "
            f"{', '.join(rule.type.shapes)}, not {result.meta.shape}"
        )
    options, weight = rule.resolved(overrides)
    return [
        Insight(
            text=_sentence(rule.template.render(**f.values)),
            score=f.score * weight,
            rule=rule.name,
            key=(facet, *f.key),
            tone=f.tone,
            component=component,
            facet=facet,
        )
        for f in rule.type.fn(result, options, ctx)
    ]


def component_insights(
    component: str,
    refs: Sequence[Any],
    facets: Sequence[Any],
    ctx: InsightContext,
    rules: Mapping[str, Rule] | None = None,
) -> list[Insight]:
    """The sentences of a component's rules on each of its facets (`facets.Facet`)."""
    rules = default_rules() if rules is None else rules
    out: list[Insight] = []
    for raw in refs:
        name, overrides = parse_ref(raw)
        for facet in facets:
            label = facet.label if len(facets) > 1 or facet.key is not None else None
            out += evaluate(rules[name], facet.result, ctx, overrides, component, label)
    return out


def collect(insights: Iterable[Insight], top: int | None = None) -> list[Insight]:
    """The best sentence per key (the same metric and group), ranked by score, the top ones."""
    best: dict[tuple, Insight] = {}
    for insight in insights:
        kept = best.get(insight.key)
        if kept is None or insight.score > kept.score:
            best[insight.key] = insight
    ranked = sorted(best.values(), key=lambda i: (-i.score, i.text))
    return ranked if top is None else ranked[:top]
