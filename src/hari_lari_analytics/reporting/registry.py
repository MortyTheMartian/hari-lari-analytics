"""The semantic layer: `metrics.yml`, `dimensions.yml` and `kpi_tree.yml`, loaded and validated.

The three files are checked against each other and against the models in `models/`: every
reference names an existing metric, dimension or model, there are no cycles, and every metric
and identity exists on at least one model. The registry then gives the query compiler the SQL
of each metric: the aggregate of a sql or filtered metric on a model, and the expression of a
derived or relative metric over the columns of other metrics.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache
from graphlib import CycleError, TopologicalSorter
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hari_lari_analytics.reporting.db import model_sql

Format = Literal["idr", "idr_compact", "count", "decimal", "pct", "timestamp"]
Additivity = Literal["full", "order_grain", "none"]
Grain = Literal["line", "order", "item", "day"]
Domain = Literal["none", "span", "all", "calendar"]
Kind = Literal["sql", "filtered", "derived", "relative"]
Value = bool | int | str
WhereValue = Value | list[Value] | None

WINDOW_VALUES = frozenset({"trading_days"})
TIME_BUCKET = "bucket"  # the time dimension in the grain of the query, computed by the compiler
TIME_DIMENSIONS = (TIME_BUCKET, "sale_date", "sale_week", "sale_month")
SQL_WORDS = frozenset(
    "and or not is null true false case when then else end in between as cast "
    "double bigint integer decimal varchar".split()
)
_AGGREGATE = re.compile(
    r"^\s*(sum|count|min|max|avg)\(\s*(distinct\s+)?(.+?)\s*\)"
    r"(?:\s*filter\s*\(\s*where\s+(.+)\))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_REFERENCE = re.compile(r"\b(total\.)?([A-Za-z_]\w*)\b(?!\s*\()")
_BIN = re.compile(r"^(\d+)(?:-(\d+)|(\+))?$")


class RegistryError(ValueError):
    """A registry file is invalid; the message names the file and the entry."""


# --- the YAML schemas -------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilteredSpec(_Strict):
    base: str
    where: dict[str, WhereValue] = Field(min_length=1)


class MetricSpec(_Strict):
    label: str | None = None
    description: str | None = None
    format: Format | None = None
    up_is_good: bool | None = None
    additivity: Additivity | None = None
    min_n: dict[str, int] | None = None
    sql: dict[str, str] | None = None
    filtered: FilteredSpec | None = None
    derived: str | None = None
    relative: str | None = None


class MetricDefaults(_Strict):
    min_n: dict[str, int] = {"orders": 30}


class SortSpec(_Strict):
    by: str = "value"
    desc: bool = False


class Dimension(_Strict):
    id: str
    label: str
    description: str | None = None
    grain: Grain
    models: list[str] = Field(min_length=1)
    column: str | dict[str, str] | None = None
    sort: SortSpec = SortSpec()
    domain: Domain = "none"
    values: list[Value] | None = None
    labels: dict[Value, str] = {}
    null_label: str | None = None
    bins: list[int | str] | None = None

    @field_validator("bins")
    @classmethod
    def _bins(cls, bins):
        if bins is not None:
            parse_bins(bins)
        return bins

    def column_on(self, model: str) -> str:
        if model not in self.models:
            raise RegistryError(f"dimension {self.id} is not on {model}")
        if isinstance(self.column, dict):
            return self.column.get(model, self.id)
        return self.column or self.id

    @property
    def bin_list(self) -> list[Bin]:
        return parse_bins(self.bins) if self.bins else []


class Identity(_Strict):
    id: str
    description: str | None = None
    kpi: str
    kind: Literal["product", "sum", "sum_over"]
    drivers: list[str] = []
    dimension: str | None = None
    driver: str | None = None
    then: str | None = None

    @property
    def terms(self) -> list[tuple[int, str]]:
        """The drivers of a product or sum with their sign: [(1, 'gross_sales'), (-1, ...)]."""
        return [(-1, d[1:]) if d.startswith("-") else (1, d) for d in self.drivers]

    @property
    def metrics(self) -> list[str]:
        return [self.kpi, *(m for _, m in self.terms), *([self.driver] if self.driver else [])]


# --- resolved objects ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bin:
    label: str
    low: int
    high: int | None  # inclusive; None for the open last bin


def parse_bins(items: Iterable[int | str]) -> list[Bin]:
    bins: list[Bin] = []
    items = list(items)
    for i, item in enumerate(items):
        m = _BIN.match(str(item))
        if not m:
            raise ValueError(f"bin {item!r}: use a number, a range such as '2-3' or 'N+'")
        low = int(m[1])
        high = None if m[3] else int(m[2] or m[1])
        if high is None and i < len(items) - 1:
            raise ValueError(f"bin {item!r}: only the last bin may be open")
        if high is not None and high < low:
            raise ValueError(f"bin {item!r}: the range is reversed")
        if bins and low <= bins[-1].high:
            raise ValueError(f"bin {item!r}: bins must be ascending and must not overlap")
        bins.append(Bin(str(item), low, high))
    return bins


@dataclass(frozen=True)
class Aggregate:
    func: str
    expr: str
    distinct: bool = False
    where: str | None = None

    @classmethod
    def parse(cls, text: str) -> Aggregate:
        m = _AGGREGATE.match(text)
        if not m:
            raise ValueError(
                f"{text!r} is not an aggregate: use func(expr), func(distinct expr) or "
                "func(expr) filter (where condition), with func sum, count, min, max or avg"
            )
        return cls(m[1].lower(), m[3].strip(), bool(m[2]), m[4].strip() if m[4] else None)

    def restrict(self, condition: str) -> Aggregate:
        where = condition if self.where is None else f"({self.where}) and ({condition})"
        return Aggregate(self.func, self.expr, self.distinct, where)

    @property
    def sql(self) -> str:
        text = f"{self.func}({'distinct ' if self.distinct else ''}{self.expr})"
        return f"{text} filter (where {self.where})" if self.where else text


@dataclass(frozen=True)
class Metric:
    id: str
    kind: Kind
    label: str
    description: str
    format: Format
    up_is_good: bool | None  # None: neutral, the change is not coloured
    additivity: Additivity
    min_n: dict[str, int]
    aggregates: dict[str, Aggregate]  # sql and filtered: per model
    expression: str | None  # derived and relative
    inputs: frozenset[str]  # metrics the expression uses as values of the group
    totals: frozenset[str]  # metrics the expression uses as total.<metric>
    models: frozenset[str]  # the models the metric exists on

    @property
    def depends_on(self) -> frozenset[str]:
        return self.inputs | self.totals


@dataclass(frozen=True)
class MetricPlan:
    """What the compiler computes for a set of metrics on one model.

    `aggregates` are computed per group in SQL; `computed` are the derived and relative metrics
    in an order in which every input comes first. A relative metric needs the total row of the
    metrics it reads as `total.<metric>`.
    """

    aggregates: list[str]
    computed: list[str]


def sql_literal(value: Value | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


# --- the registry -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Registry:
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    identities: dict[str, Identity]
    order: tuple[str, ...]  # every metric after the metrics it depends on

    def metric(self, metric_id: str) -> Metric:
        try:
            return self.metrics[metric_id]
        except KeyError:
            raise RegistryError(f"unknown metric {metric_id!r}") from None

    def dimension(self, dimension_id: str) -> Dimension:
        try:
            return self.dimensions[dimension_id]
        except KeyError:
            raise RegistryError(f"unknown dimension {dimension_id!r}") from None

    def identity(self, identity_id: str) -> Identity:
        try:
            return self.identities[identity_id]
        except KeyError:
            raise RegistryError(f"unknown KPI-tree identity {identity_id!r}") from None

    def aggregate(self, metric_id: str, model: str) -> str:
        """The SQL aggregate of a sql or filtered metric on a model."""
        metric = self.metric(metric_id)
        if metric.kind not in ("sql", "filtered"):
            raise RegistryError(f"metric {metric_id} is {metric.kind}, not an aggregate")
        self._require(metric, model)
        return metric.aggregates[model].sql

    def expression(
        self,
        metric_id: str,
        ref: Callable[[str], str] = lambda m: m,
        total: Callable[[str], str] = lambda m: f"total_{m}",
        trading_days: str = "trading_days",
    ) -> str:
        """The SQL of a derived or relative metric, with each reference written by `ref`,
        each `total.<metric>` by `total`, and `trading_days` replaced."""
        metric = self.metric(metric_id)
        if metric.expression is None:
            raise RegistryError(f"metric {metric_id} is {metric.kind}, not an expression")
        return render_expression(metric.expression, ref, total, trading_days)

    def plan(self, metric_ids: Iterable[str], model: str) -> MetricPlan:
        """Every metric needed for `metric_ids` on `model`, in the order to compute them."""
        needed: set[str] = set()
        stack = list(metric_ids)
        while stack:
            metric = self.metric(stack.pop())
            self._require(metric, model)
            if metric.id not in needed:
                needed.add(metric.id)
                stack.extend(metric.depends_on)
        ordered = [m for m in self.order if m in needed]
        return MetricPlan(
            aggregates=[m for m in ordered if self.metrics[m].kind in ("sql", "filtered")],
            computed=[m for m in ordered if self.metrics[m].kind in ("derived", "relative")],
        )

    def metrics_on(self, model: str) -> list[str]:
        return [m for m in self.order if model in self.metrics[m].models]

    def _require(self, metric: Metric, model: str) -> None:
        if model not in metric.models:
            where = ", ".join(sorted(metric.models))
            raise RegistryError(f"metric {metric.id} is not on {model} (only on {where})")


def render_expression(
    expression: str,
    ref: Callable[[str], str],
    total: Callable[[str], str],
    trading_days: str = "trading_days",
) -> str:
    def replace(m: re.Match) -> str:
        name = m[2]
        if m[1]:
            return total(name)
        if name in WINDOW_VALUES:
            return trading_days
        if name.lower() in SQL_WORDS:
            return m[0]
        return ref(name)

    return _REFERENCE.sub(replace, expression)


def _references(expression: str) -> tuple[set[str], set[str]]:
    """The metric names an expression uses, and those it uses as total.<metric>."""
    if any(c in expression for c in "'\";") or "--" in expression:
        raise ValueError("quotes, semicolons and comments are not allowed in an expression")
    inputs: set[str] = set()
    totals: set[str] = set()
    for m in _REFERENCE.finditer(expression):
        name = m[2]
        if m[1]:
            totals.add(name)
        elif name not in WINDOW_VALUES and name.lower() not in SQL_WORDS:
            inputs.add(name)
    return inputs, totals


# --- loading ----------------------------------------------------------------------------------


def _read(name: str, text: str | None, empty: dict | list):
    if text is None:
        text = (resources.files("hari_lari_analytics.reporting") / name).read_text()
    data = yaml.safe_load(text)
    return empty if data is None else data


def _fail(where: str, error: Exception) -> RegistryError:
    if isinstance(error, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in error.errors()
        )
        return RegistryError(f"{where}: {details}")
    return RegistryError(f"{where}: {error}")


def load_registry(
    metrics: str | None = None,
    dimensions: str | None = None,
    kpi_tree: str | None = None,
    models: Iterable[str] | None = None,
) -> Registry:
    """Load and validate the registries; the texts default to the packaged YAML files."""
    models = frozenset(models if models is not None else model_sql())
    dims = _load_dimensions(_read("dimensions.yml", dimensions, {}), models)
    metric_map, order = _load_metrics(_read("metrics.yml", metrics, {}), dims, models)
    identities = _load_identities(_read("kpi_tree.yml", kpi_tree, []), metric_map, dims)
    return Registry(metric_map, dims, identities, order)


@cache
def default_registry() -> Registry:
    return load_registry()


def _load_dimensions(raw: dict, models: frozenset[str]) -> dict[str, Dimension]:
    if not isinstance(raw, dict):
        raise RegistryError("dimensions.yml: expected a mapping of dimension ids")
    dims: dict[str, Dimension] = {}
    for dim_id, spec in raw.items():
        where = f"dimensions.yml: {dim_id}"
        try:
            dim = Dimension.model_validate({"id": dim_id, **(spec or {})})
        except (ValidationError, ValueError) as error:
            raise _fail(where, error) from None
        unknown = sorted(set(dim.models) - models)
        if unknown:
            raise RegistryError(f"{where}: unknown model(s) {', '.join(unknown)}")
        if isinstance(dim.column, dict) and set(dim.column) - set(dim.models):
            raise RegistryError(f"{where}: column names a model that is not in models")
        if dim.domain == "all" and not dim.values:
            raise RegistryError(f"{where}: domain all needs the list of values")
        if dim.bins and dim.domain != "none":
            raise RegistryError(f"{where}: a binned dimension has its bins as domain")
        if dim.domain == "calendar" and dim_id not in TIME_DIMENSIONS:
            raise RegistryError(
                f"{where}: domain calendar is only for the time dimensions "
                f"{', '.join(TIME_DIMENSIONS)}"
            )
        if dim.grain == "day" and dim_id != TIME_BUCKET and "dim_calendar" not in dim.models:
            raise RegistryError(
                f"{where}: a dimension of grain day must also be on dim_calendar, "
                "to count the trading days per value"
            )
        dims[dim_id] = dim
    return dims


def _metric_specs(raw: dict) -> tuple[MetricDefaults, dict[str, MetricSpec]]:
    if not isinstance(raw, dict):
        raise RegistryError("metrics.yml: expected a mapping of metric ids")
    raw = dict(raw)
    try:
        defaults = MetricDefaults.model_validate(raw.pop("defaults", None) or {})
    except ValidationError as error:
        raise _fail("metrics.yml: defaults", error) from None
    specs = {}
    for metric_id, spec in raw.items():
        try:
            specs[metric_id] = MetricSpec.model_validate(spec or {})
        except ValidationError as error:
            raise _fail(f"metrics.yml: {metric_id}", error) from None
    return defaults, specs


def _where_sql(where: dict[str, WhereValue], dims: dict[str, Dimension], model: str) -> str:
    parts = []
    for dim_id, value in where.items():
        column = dims[dim_id].column_on(model)
        if value is None:
            parts.append(f"{column} is null")
        elif isinstance(value, list):
            parts.append(f"{column} in ({', '.join(sql_literal(v) for v in value)})")
        else:
            parts.append(f"{column} = {sql_literal(value)}")
    return " and ".join(parts)


REQUIRED = ("label", "description", "format", "up_is_good", "additivity")


def _file_order(graph: dict[str, set[str]]) -> tuple[str, ...]:
    """The metrics in file order, each moved after the metrics it depends on (no cycles)."""
    order: list[str] = []

    def visit(metric_id: str) -> None:
        if metric_id not in order:
            for dependency in sorted(graph[metric_id]):
                visit(dependency)
            order.append(metric_id)

    for metric_id in graph:
        visit(metric_id)
    return tuple(order)


def _load_metrics(
    raw: dict, dims: dict[str, Dimension], models: frozenset[str]
) -> tuple[dict[str, Metric], tuple[str, ...]]:
    defaults, specs = _metric_specs(raw)

    graph: dict[str, set[str]] = {}
    refs: dict[str, tuple[set[str], set[str]]] = {}
    for metric_id, spec in specs.items():
        where = f"metrics.yml: {metric_id}"
        kinds = [k for k in ("sql", "filtered", "derived", "relative") if getattr(spec, k)]
        if len(kinds) != 1:
            raise RegistryError(
                f"{where}: needs exactly one of sql, filtered, derived and relative"
                + (f", not {' and '.join(kinds)}" if kinds else "")
            )
        kind = kinds[0]
        if kind != "filtered":
            missing = [f for f in REQUIRED if f not in spec.model_fields_set]
            if missing:
                raise RegistryError(f"{where}: missing {', '.join(missing)}")
        if kind == "filtered":
            inputs, totals = {spec.filtered.base}, set()
            for dim_id in spec.filtered.where:
                if dim_id not in dims:
                    raise RegistryError(f"{where}: unknown dimension {dim_id} in where")
                if dims[dim_id].bins:
                    raise RegistryError(f"{where}: cannot filter on the binned {dim_id}")
                if dim_id == TIME_BUCKET:
                    raise RegistryError(f"{where}: cannot filter on {TIME_BUCKET}")
        elif kind in ("derived", "relative"):
            try:
                inputs, totals = _references(getattr(spec, kind))
            except ValueError as error:
                raise _fail(where, error) from None
            if kind == "derived" and totals:
                raise RegistryError(f"{where}: total.<metric> is only allowed in relative")
            if kind == "relative" and not totals:
                raise RegistryError(f"{where}: a relative metric must use total.<metric>")
        else:
            inputs, totals = set(), set()
        unknown = sorted((inputs | totals) - set(specs))
        if unknown:
            raise RegistryError(f"{where}: unknown metric(s) {', '.join(unknown)}")
        refs[metric_id] = (inputs, totals)
        graph[metric_id] = inputs | totals
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        cycle = " -> ".join(error.args[1])
        raise RegistryError(f"metrics.yml: circular definition {cycle}") from None
    order = _file_order(graph)

    metrics: dict[str, Metric] = {}
    for metric_id in order:
        spec = specs[metric_id]
        where = f"metrics.yml: {metric_id}"
        inputs, totals = refs[metric_id]
        aggregates: dict[str, Aggregate] = {}
        expression = None
        if spec.sql:
            unknown = sorted(set(spec.sql) - models)
            if unknown:
                raise RegistryError(f"{where}: unknown model(s) {', '.join(unknown)}")
            try:
                aggregates = {m: Aggregate.parse(text) for m, text in spec.sql.items()}
            except ValueError as error:
                raise _fail(where, error) from None
            kind: Kind = "sql"
            base = None
        elif spec.filtered:
            kind = "filtered"
            base = metrics[spec.filtered.base]
            if base.kind not in ("sql", "filtered"):
                raise RegistryError(f"{where}: the base {base.id} must be a sql or filtered metric")
            for model, agg in base.aggregates.items():
                if all(model in dims[d].models for d in spec.filtered.where):
                    aggregates[model] = agg.restrict(_where_sql(spec.filtered.where, dims, model))
        else:
            kind = "derived" if spec.derived else "relative"
            expression = spec.derived or spec.relative
            base = None
        if aggregates:
            available = frozenset(aggregates)
        else:
            available = frozenset(models)
            for other in inputs | totals:
                available &= metrics[other].models

        def inherit(field: str, base=base, spec=spec):
            if field in spec.model_fields_set or base is None:
                return getattr(spec, field)
            return getattr(base, field)

        if kind == "filtered" and spec.label is None:
            raise RegistryError(f"{where}: missing label")
        min_n = spec.min_n if spec.min_n is not None else (
            base.min_n if base is not None else dict(defaults.min_n)
        )  # fmt: skip
        metric = Metric(
            id=metric_id,
            kind=kind,
            label=spec.label,
            description=inherit("description") or "",
            format=inherit("format"),
            up_is_good=inherit("up_is_good"),
            additivity=inherit("additivity"),
            min_n=min_n,
            aggregates=aggregates,
            expression=expression,
            inputs=frozenset(inputs),
            totals=frozenset(totals),
            models=available,
        )
        if not metric.models:
            raise RegistryError(f"{where}: its inputs share no model")
        metrics[metric_id] = metric

    for metric in metrics.values():
        where = f"metrics.yml: {metric.id}"
        for count, n in metric.min_n.items():
            other = metrics.get(count)
            if other is None or other.format != "count":
                raise RegistryError(f"{where}: min_n uses {count}, which is not a count metric")
            if n < 1:
                raise RegistryError(f"{where}: min_n of {count} must be at least 1")
            if not metric.models <= other.models:
                raise RegistryError(f"{where}: min_n uses {count}, which is not on every model")
    for dim in dims.values():
        if dim.sort.by != "value" and dim.sort.by not in metrics:
            raise RegistryError(f"dimensions.yml: {dim.id}: sort by unknown metric {dim.sort.by}")
    return metrics, order


def _load_identities(
    raw: list, metrics: dict[str, Metric], dims: dict[str, Dimension]
) -> dict[str, Identity]:
    if not isinstance(raw, list):
        raise RegistryError("kpi_tree.yml: expected a list of identities")
    identities: dict[str, Identity] = {}
    for i, spec in enumerate(raw):
        try:
            identity = Identity.model_validate(spec)
        except ValidationError as error:
            raise _fail(f"kpi_tree.yml: identity {i + 1}", error) from None
        where = f"kpi_tree.yml: {identity.id}"
        if identity.id in identities:
            raise RegistryError(f"{where}: the id is used twice")
        if identity.kind == "sum_over":
            if not identity.dimension or not identity.driver or identity.drivers:
                raise RegistryError(f"{where}: sum_over needs dimension and driver, not drivers")
            if identity.dimension not in dims:
                raise RegistryError(f"{where}: unknown dimension {identity.dimension}")
        else:
            if identity.dimension or identity.driver or identity.then:
                raise RegistryError(
                    f"{where}: dimension, driver and then belong to sum_over identities"
                )
            if len(identity.drivers) < 2:
                raise RegistryError(f"{where}: needs at least two drivers")
            if identity.kind == "product" and any(d.startswith("-") for d in identity.drivers):
                raise RegistryError(f"{where}: a product has no negative drivers")
        unknown = [m for m in identity.metrics if m not in metrics]
        if unknown:
            raise RegistryError(f"{where}: unknown metric(s) {', '.join(unknown)}")
        if identity.kind == "sum_over" and metrics[identity.driver].additivity != "full":
            raise RegistryError(
                f"{where}: the driver {identity.driver} must be fully additive to sum over "
                f"{identity.dimension}"
            )
        if not _shared_models(identity, metrics, dims):
            raise RegistryError(f"{where}: its metrics share no model")
        identities[identity.id] = identity
    for identity in identities.values():
        if identity.then is None:
            continue
        where = f"kpi_tree.yml: {identity.id}"
        then = identities.get(identity.then)
        if then is None:
            raise RegistryError(f"{where}: then names the unknown identity {identity.then}")
        if then.kind == "sum_over" or then.kpi != identity.driver:
            raise RegistryError(
                f"{where}: then must be a product or sum identity of {identity.driver}"
            )
    return identities


def _shared_models(
    identity: Identity, metrics: dict[str, Metric], dims: dict[str, Dimension]
) -> frozenset[str]:
    shared = frozenset.intersection(*(metrics[m].models for m in identity.metrics))
    if identity.dimension:
        shared &= frozenset(dims[identity.dimension].models)
    return shared


def identity_models(registry: Registry, identity: Identity) -> frozenset[str]:
    """The models on which every metric (and the dimension) of an identity exists."""
    return _shared_models(identity, registry.metrics, registry.dimensions)
