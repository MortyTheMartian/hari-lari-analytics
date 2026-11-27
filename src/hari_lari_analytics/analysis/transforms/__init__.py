"""The transform library: pure functions from `Result` to `Result` that never query the database.

A transform is `(result, options, ctx) -> Result` with a pydantic options model, registered with
`@transform("name", options=Model, ...)`. It is used in a component as a list entry, either a
bare name (`- per_trading_day`) or a name with options (`- pivot: {rows: weekday, ...}`), and
the steps run in the order listed.

A transform can declare what it needs from the query (`requires`): extra buckets before the
window, metrics, dimensions or a comparison. The component loader (plan 7.12) calls
`plan_query`, which adds what can be added (buckets before the window, metrics) and fails with
a clear message on what cannot (a dimension, a comparison). A transform also declares the
result shapes it accepts, and `apply` refuses any other shape before running it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from hari_lari_analytics.analysis.compiler import Query
from hari_lari_analytics.analysis.result import Result, Shape
from hari_lari_analytics.analysis.windows import Calendar
from hari_lari_analytics.semantic.registry import Registry, RegistryError, default_registry

ALL_SHAPES: tuple[Shape, ...] = ("single", "rows", "series", "matrix", "decomposition")


class TransformError(ValueError):
    """A transform cannot run: unknown name, wrong options, wrong shape or a missing column."""


class Options(BaseModel):
    """Base of the options models: an unknown option is an error, as everywhere in the YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoOptions(Options):
    pass


@dataclass(frozen=True)
class TransformContext:
    """What a transform may read besides the result: the registry and the run's calendar.

    Neither gives access to the database; the calendar holds the trading days in memory.
    """

    registry: Registry = field(default_factory=default_registry)
    calendar: Calendar | None = None


@dataclass(frozen=True)
class Requirements:
    """What a transform needs from the query that produced its result."""

    buckets_before: int = 0  # added: at least this many buckets before the window
    metrics: tuple[str, ...] = ()  # added when the query lacks them
    dimensions: tuple[str, ...] = ()  # checked: the query must group by them
    compare: bool = False  # checked: the query must ask for a comparison
    totals: bool = False  # added: the total row
    # added or checked: every group of both windows is in the result (keep_prior_only, or a
    # top N with its "Other" row), so that the groups add up to the total
    all_groups: bool = False
    only_dimensions: tuple[str, ...] | None = None  # checked: exactly these dimensions
    sums: tuple[str, ...] = ()  # checked: metrics summed across rows must add up over the dims


Fn = Callable[[Result, Any, TransformContext], Result]
Requires = Callable[[Any, Registry], Requirements]


@dataclass(frozen=True)
class Transform:
    name: str
    fn: Fn
    options: type[BaseModel]
    shapes: tuple[Shape, ...]
    produces: Shape | None  # the shape of the output; None: the same as the input
    requires: Requires | None
    description: str


TRANSFORMS: dict[str, Transform] = {}


def transform(
    name: str,
    options: type[BaseModel] = NoOptions,
    shapes: Iterable[Shape] = ("single", "rows", "series"),
    produces: Shape | None = None,
    requires: Requires | None = None,
) -> Callable[[Fn], Fn]:
    """Register a transform under `name`; its docstring is its description.

    `requires(options, registry)` returns the `Requirements` of the transform on its query."""

    def register(fn: Fn) -> Fn:
        if name in TRANSFORMS:
            raise ValueError(f"the transform {name} is registered twice")
        doc = (fn.__doc__ or "").strip().splitlines()
        TRANSFORMS[name] = Transform(
            name, fn, options, tuple(shapes), produces, requires, doc[0] if doc else ""
        )
        return fn

    return register


Step = tuple[str, dict]


def parse_step(raw: str | dict) -> Step:
    """One entry of a component's `transforms` list: `name` or `{name: options}`."""
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict) and len(raw) == 1:
        name, options = next(iter(raw.items()))
        if options is None:
            options = {}
        if isinstance(name, str) and isinstance(options, dict):
            return name, options
    raise TransformError(
        f"transforms: {raw!r} is not a transform; write a name or a name with options"
    )


def get(name: str) -> Transform:
    if name not in TRANSFORMS:
        raise TransformError(f"unknown transform {name!r} (known: {', '.join(sorted(TRANSFORMS))})")
    return TRANSFORMS[name]


def parse_options(name: str, raw: dict) -> BaseModel:
    spec = get(name)
    try:
        return spec.options.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'options'}: {e['msg']}" for e in error.errors()
        )
        raise TransformError(f"transform {name}: {details}") from None


def apply(name: str, raw_options: dict | None, result: Result, ctx: TransformContext) -> Result:
    """Run one transform on a result, after checking its options and the result shape."""
    spec = get(name)
    options = parse_options(name, raw_options or {})
    if result.meta.shape not in spec.shapes:
        raise TransformError(
            f"transform {name} does not work on a result of shape {result.meta.shape} "
            f"(it accepts {', '.join(spec.shapes)})"
        )
    try:
        out = spec.fn(result, options, ctx)
    except TransformError:
        raise
    except KeyError as error:
        raise TransformError(
            f"transform {name}: the result has no column {error.args[0]}"
        ) from None
    return out


def run_pipeline(result: Result, steps: Iterable[str | dict], ctx: TransformContext) -> Result:
    """Run the steps of a component in order."""
    for raw in steps:
        name, options = parse_step(raw)
        result = apply(name, options, result, ctx)
    return result


def plan_query(
    query: Query, steps: Iterable[str | dict], registry: Registry | None = None
) -> Query:
    """The query with what its transforms require added, or a `TransformError` if it cannot."""
    registry = registry or default_registry()
    buckets_before = query.buckets_before
    metrics = list(query.metrics)
    totals, keep_prior_only = query.totals, query.keep_prior_only
    for raw in steps:
        name, raw_options = parse_step(raw)
        spec = get(name)
        if spec.requires is None:
            parse_options(name, raw_options)
            continue
        need = spec.requires(parse_options(name, raw_options), registry)
        if need.compare and query.compare == "none":
            raise TransformError(f"transform {name} needs a comparison: set `compare` on the query")
        grouped = ", ".join(query.dimensions) or "none"
        for dim in need.dimensions:
            if dim not in query.dimensions:
                raise TransformError(
                    f"transform {name} needs the dimension {dim}, "
                    f"which the query does not group by ({grouped})"
                )
        if need.only_dimensions is not None and list(need.only_dimensions) != query.dimensions:
            wanted = ", ".join(need.only_dimensions) or "no dimension"
            raise TransformError(
                f"transform {name} needs a query grouped by {wanted}, not by {grouped}"
            )
        if need.all_groups:
            if query.top is not None and not query.top.other:
                raise TransformError(
                    f"transform {name} needs every group, so that the groups add up to the "
                    "total: with `top`, set `other: true`"
                )
            keep_prior_only = keep_prior_only or query.top is None
        for metric in need.sums:
            try:
                problem = registry.sum_problem(metric, query.dimensions)
            except RegistryError as error:
                raise TransformError(f"transform {name}: {error}") from None
            if problem:
                raise TransformError(f"transform {name}: {problem}")
        totals = totals or need.totals
        buckets_before = max(buckets_before, need.buckets_before)
        metrics += [m for m in need.metrics if m not in metrics]
    update = {
        "buckets_before": buckets_before,
        "metrics": metrics,
        "totals": totals,
        "keep_prior_only": keep_prior_only,
    }
    if all(getattr(query, k) == v for k, v in update.items()):
        return query
    return query.model_copy(update=update)


# --- helpers shared by the transforms -----------------------------------------------------------


def need_columns(result: Result, name: str, *columns: str) -> None:
    missing = [c for c in columns if c not in result.df.columns]
    if missing:
        have = ", ".join(result.df.columns)
        raise TransformError(f"transform {name}: no column {', '.join(missing)} (columns: {have})")


def dimension_columns(result: Result) -> list[str]:
    """The dimension columns of a result, in the order of the query."""
    return [d for d in result.meta.dimensions if d in result.df.columns]


def time_dimension(result: Result) -> str | None:
    for dim_id, dim in result.meta.dimensions.items():
        if dim.domain == "calendar" and dim_id in result.df.columns:
            return dim_id
    return None


from hari_lari_analytics.analysis.transforms import (  # noqa: E402, F401
    classify,
    decompose,
    quality,
    shape,
    share,
    time,
)
