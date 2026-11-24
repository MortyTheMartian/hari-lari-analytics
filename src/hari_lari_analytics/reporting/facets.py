"""Facets: one component repeated inside one slot, per dimension value, per period or per metric.

```yaml
facet:
  by: adjusted_category             # a dimension, or `period`, or `metric`
  values: { top: 5, by: net_sales }  # or `all`, or a list such as [Coffee, Mains]
  layout: grid(2x3)                 # row, column or grid(<rows>x<columns>)
```

- **By dimension**: the component runs once per value, with the value added to its `where`.
  `all` takes every value sold in the component's window, in the dimension's order; `top` the
  first N by a metric over that window. Both are decided once, before the facets run, so all
  facets show the same values in the same order.
- **By period**: binds `$period` to each window of the list, for the three period cards.
- **By metric**: runs once and hands one metric per facet to the visual (`all`: every metric of
  the query), for the KPI cards.

`run_facets` returns one `Facet` per cell with its label; a component without a facet gives one
facet without a label, so the builder treats every slot alike. The layout splits the slot: a
grid has a fixed number of cells, so it refuses `all` values of a dimension, which could be more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hari_lari_analytics.reporting.compiler import (
    Query,
    QueryError,
    TopSpec,
    compile_query,
    run_query,
)
from hari_lari_analytics.reporting.queries import QueryContext, SourceQuery
from hari_lari_analytics.reporting.registry import TIME_BUCKET, Registry, RegistryError
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.windows import Calendar, parse_window

if TYPE_CHECKING:
    from hari_lari_analytics.reporting.components import Component

_GRID = re.compile(r"^grid\((\d+)\s*x\s*(\d+)\)$")
PERIOD, METRIC = "period", "metric"

FacetValue = bool | int | float | str | None


class FacetError(ValueError):
    """A facet is invalid or cannot run."""


class TopValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top: int = Field(ge=1)
    by: str  # a metric, ranked over the component's window


class FacetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    by: str  # a dimension, `period` or `metric`
    values: Literal["all"] | list[FacetValue] | TopValues = "all"
    layout: str = "row"  # row, column or grid(<rows>x<columns>)

    @field_validator("layout")
    @classmethod
    def _layout(cls, layout: str) -> str:
        m = _GRID.match(layout)
        if layout not in ("row", "column") and not (m and int(m[1]) >= 1 and int(m[2]) >= 1):
            raise ValueError(f"{layout!r}: use row, column or grid(<rows>x<columns>)")
        return layout

    @property
    def cells(self) -> int | None:
        """The number of cells of a grid; None for a row or column, which take any number."""
        m = _GRID.match(self.layout)
        return int(m[1]) * int(m[2]) if m else None

    @property
    def grid(self) -> tuple[int, int] | None:
        """(rows, columns) of a grid layout."""
        m = _GRID.match(self.layout)
        return (int(m[1]), int(m[2])) if m else None


@dataclass(frozen=True)
class Facet:
    """One cell of a slot: the value, window or metric it shows, its label and its result."""

    key: Any  # the dimension value, the window name or the metric id; None without a facet
    label: str | None
    result: Result
    metric: str | None = None  # by metric: the metric this cell shows


# --- the check, without data ----------------------------------------------------------------------


def check_facet(
    facet: FacetSpec,
    query: Query | SourceQuery,
    parameters: set[str],
    registry: Registry,
    calendar: Calendar,
) -> None:
    """Check a facet against the component's (planned) query; raise a `FacetError`."""
    values = facet.values
    count = len(values) if isinstance(values, list) else None
    if facet.by == PERIOD:
        if not isinstance(values, list) or not values:
            raise FacetError("by period needs a list of windows, such as [L14D, L6W, L3M]")
        if PERIOD not in parameters:
            raise FacetError("by period binds $period, which the component does not use")
        for name in values:
            try:
                parse_window(str(name))
            except ValueError as error:
                raise FacetError(f"values: {error}") from None
    elif facet.by == METRIC:
        if isinstance(values, TopValues):
            raise FacetError("by metric takes `all` or a list of metrics, not top")
        if isinstance(query, Query):
            unknown = [m for m in values if m not in query.metrics] if count else []
            if unknown:
                raise FacetError(
                    f"values: {', '.join(map(str, unknown))} is not a metric of the query "
                    f"({', '.join(query.metrics)})"
                )
            count = count or len(query.metrics)
    else:
        if not isinstance(query, Query):
            raise FacetError(f"by {facet.by} needs a compiled query, not a Python source")
        if facet.by == TIME_BUCKET:
            raise FacetError(f"cannot facet by {TIME_BUCKET}; use a time dimension in the query")
        try:
            registry.dimension(facet.by).column_on(query.model)
        except RegistryError as error:
            raise FacetError(f"by: {error}") from None
        if values == "all" and facet.cells is not None:
            raise FacetError(
                "values: all can be more values than a grid has cells; use top: N, or a row or "
                "column layout"
            )
        if not isinstance(values, list):
            try:
                compile_query(values_query(facet, query), registry, calendar)
            except (QueryError, RegistryError) as error:
                raise FacetError(f"values: {error}") from None
        if isinstance(values, TopValues):
            count = values.top
    if facet.cells is not None and count is not None and count > facet.cells:
        raise FacetError(
            f"{count} facets do not fit into the {facet.cells} cells of {facet.layout}"
        )


# --- running --------------------------------------------------------------------------------------


def values_query(facet: FacetSpec, query: Query) -> Query:
    """The query that decides the values of a dimension facet: over the component's window and
    filters, ranked by the top metric, or every value in the dimension's order."""
    if isinstance(facet.values, TopValues):
        metric, top = facet.values.by, TopSpec(n=facet.values.top, by=facet.values.by)
    else:
        metric, top = query.metrics[0], None
    return Query(
        model=query.model,
        metrics=[metric],
        dimensions=[facet.by],
        window=query.window,
        where=query.where,
        top=top,
    )


def _python(value: Any) -> Any:
    """A value of a result as a plain Python value, for a filter (a day as a date)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        value = pd.Timestamp(value)
    if isinstance(value, pd.Timestamp):
        return value.date() if value == value.normalize() else value.to_pydatetime()
    if isinstance(value, date):
        return value
    return value.item() if hasattr(value, "item") else value


def facet_values(facet: FacetSpec, query: Query, ctx: QueryContext) -> list[Any]:
    """The values of a dimension facet, decided once."""
    if isinstance(facet.values, list):
        return list(facet.values)
    result = run_query(ctx.con, values_query(facet, query), ctx.registry, ctx.calendar)
    return [_python(v) for v in result.rows[facet.by]]


def run_facets(
    component: Component,
    ctx: QueryContext,
    params: dict[str, Any] | None = None,
    with_: dict | None = None,
) -> list[Facet]:
    """Run a component once per facet (once without a facet)."""
    params = dict(params or {})
    bound = component.bind(params, with_)
    facet = bound.spec.facet
    if facet is None:
        return [Facet(None, None, bound.run(ctx))]

    if facet.by == PERIOD:
        facets = []
        for name in facet.values:
            result = component.bind({**params, PERIOD: name}, with_).run(ctx)
            window = result.meta.window
            facets.append(Facet(name, window.current.title if window else str(name), result))
        return facets

    if facet.by == METRIC:
        result = bound.run(ctx)
        if facet.values != "all":
            chosen = facet.values
        elif isinstance(bound.spec.query, Query):
            chosen = bound.spec.query.metrics  # as written, without those the transforms added
        else:
            chosen = list(result.meta.value_metrics)
        return [Facet(m, result.meta.metrics[m].label, result, metric=m) for m in chosen]

    query = bound.query
    if not isinstance(query, Query):
        raise FacetError(f"{component.source}: facet by {facet.by} needs a compiled query")
    dim = ctx.registry.dimension(facet.by)
    facets = []
    for value in facet_values(facet, query, ctx):
        where = {**query.where, facet.by: value}
        result = bound.with_query(query.model_copy(update={"where": where})).run(ctx)
        facets.append(Facet(value, dim.value_label(value), result))
    return facets
