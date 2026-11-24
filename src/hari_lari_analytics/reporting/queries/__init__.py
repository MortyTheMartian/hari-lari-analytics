"""The `@query` escape hatch: Python sources for results that are not SQL aggregates.

A component uses one with `query: { source: notes }` (plus `options` if the source has any).
Each source is a function `(ctx, options) -> Result`, registered with `@query("name")` and an
optional pydantic options model. `execute` runs either kind of query.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict, ValidationError

from hari_lari_analytics.load_api import ApiRun
from hari_lari_analytics.reporting.checks import CheckReport
from hari_lari_analytics.reporting.compiler import Query, QueryError, run_query
from hari_lari_analytics.reporting.registry import Registry, default_registry
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.windows import Calendar


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceQuery(_Strict):
    source: str
    options: dict[str, Any] = {}


class NoOptions(_Strict):
    pass


@dataclass(frozen=True)
class QueryContext:
    """What a query runs against: the models of one run, the registry and the calendar."""

    con: duckdb.DuckDBPyConnection
    registry: Registry
    calendar: Calendar
    run: ApiRun | None = None
    checks: CheckReport | None = None

    @classmethod
    def create(
        cls,
        con: duckdb.DuckDBPyConnection,
        registry: Registry | None = None,
        run: ApiRun | None = None,
        checks: CheckReport | None = None,
    ) -> QueryContext:
        return cls(con, registry or default_registry(), Calendar.from_db(con), run, checks)


Source = Callable[[QueryContext, BaseModel], Result]
SOURCES: dict[str, tuple[Source, type[BaseModel]]] = {}


def query(name: str, options: type[BaseModel] = NoOptions) -> Callable[[Source], Source]:
    """Register a Python source under `name`."""

    def register(fn: Source) -> Source:
        if name in SOURCES:
            raise ValueError(f"the query source {name} is registered twice")
        SOURCES[name] = (fn, options)
        return fn

    return register


def parse_query(raw: dict) -> Query | SourceQuery:
    """A query of either kind from its YAML mapping: `source` marks a Python source."""
    try:
        return SourceQuery.model_validate(raw) if "source" in raw else Query.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in error.errors()
        )
        raise QueryError(f"query: {details}") from None


def execute(spec: Query | SourceQuery, ctx: QueryContext) -> Result:
    """Run a compiled query or a registered Python source."""
    if isinstance(spec, Query):
        return run_query(ctx.con, spec, ctx.registry, ctx.calendar)
    if spec.source not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        raise QueryError(f"query: unknown source {spec.source!r} (known: {known})")
    fn, options = SOURCES[spec.source]
    try:
        parsed = options.model_validate(spec.options)
    except ValidationError as error:
        raise QueryError(f"query: options of {spec.source}: {error}") from None
    return fn(ctx, parsed)


from hari_lari_analytics.reporting.queries import lines_export, notes  # noqa: E402, F401
