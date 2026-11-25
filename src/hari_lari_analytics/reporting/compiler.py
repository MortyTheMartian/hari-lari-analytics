"""The query compiler: a semantic query becomes one SQL statement, run in DuckDB, as a `Result`.

A query names a model, metrics, dimensions, a window and a comparison, plus optional filters,
totals, a top N, bins and buckets before the window (see docs/components.md, "Queries and the
compiler"). The model's entry in `models.yml` gives the date column the windows filter on,
whether trading days apply and the count of the sample-size columns. The statement computes
the current and the comparison window together:

1. the rows of each window, with the dimensions as `_t` (the time bucket) and `_n0, _n1, ...`;
   a top N maps the other groups to "Other (k)" or drops them;
2. the aggregates per group, with the total rows through `GROUPING SETS`, so orders and every
   ratio are recomputed from the rows and never summed (`_lvl`: 0 a group, 1 the total of a
   time bucket, 2 the total of the window);
3. the frame of output rows (every bucket of a series, for every group), with the current and
   the prior aggregates joined; buckets of a series are matched with the prior by position;
4. the trading days per row, from `dim_calendar`;
5. the derived and relative metrics (a relative metric reads the total of its time bucket in a
   series, otherwise the total of the window), then the changes.

`where` narrows the universe: the totals, and so every relative metric, cover what it keeps.
`focus` keeps only some groups after that: the rows and total rows shown are those of the
focused members, while a relative metric still reads the totals of the whole universe (step 2
also aggregates them, as `<side>_u`). Filter values are bound parameters. Grouping happens
here; reshaping is left to transforms.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from hari_lari_analytics.reporting.registry import (
    TIME_BUCKET,
    Bin,
    Dimension,
    Metric,
    Registry,
    RegistryError,
    SortSpec,
    parse_bins,
    render_expression,
    sql_literal,
)
from hari_lari_analytics.reporting.result import BucketInfo, Result, ResultMeta, Shape
from hari_lari_analytics.reporting.windows import (
    GRAIN_COLUMN,
    Bucket,
    Calendar,
    Compare,
    DayEvent,
    Grain,
    ResolvedWindow,
    Window,
    bucket_before,
    buckets,
    resolve,
)

TIME_GRAIN: dict[str, Grain] = {column: grain for grain, column in GRAIN_COLUMN.items()}
AGGREGATE_KINDS = ("sql", "filtered")
ADDITIVE_FUNCS = ("sum", "count")  # an empty group is 0, not NULL

FilterValue = bool | int | float | str | date
WhereValue = FilterValue | list[FilterValue] | None


class QueryError(ValueError):
    """A query is invalid; the message says which field and why."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TopSpec(_Strict):
    n: int = Field(ge=1)
    by: str
    other: bool = False  # group the rest as "Other (k)"


class Query(_Strict):
    model: str
    metrics: list[str] = Field(min_length=1)
    dimensions: list[str] = []
    window: str | None = None  # required on a model with a time column, refused on a snapshot
    compare: Compare = "none"
    where: dict[str, WhereValue] = {}
    focus: dict[str, WhereValue] = {}
    totals: bool = False
    keep_prior_only: bool = False
    top: TopSpec | None = None
    grain: Grain | None = None
    bins: dict[str, list[int | str]] = {}
    buckets_before: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class CompiledQuery:
    """The SQL of a query with its parameters, and what is needed to wrap the result."""

    sql: str
    params: dict
    query: Query
    window: ResolvedWindow | None  # None on a model without a time column (a snapshot)
    metrics: dict[str, Metric]  # every metric and n_ column of the result, by column
    dimensions: dict[str, Dimension]
    shape: Shape
    grain: Grain | None
    buckets: tuple[BucketInfo, ...]
    prior_buckets: tuple[BucketInfo, ...]
    events: tuple[DayEvent, ...] = ()

    def run(self, con: duckdb.DuckDBPyConnection) -> Result:
        return self.wrap(con.execute(self.sql, self.params).df())

    def wrap(self, df: pd.DataFrame) -> Result:
        grand = df[df["_lvl"] == 2]
        if len(grand) != 1:
            raise RuntimeError(f"the query returned {len(grand)} total rows instead of one")
        total = grand.iloc[0]
        if self.query.dimensions and not self.query.totals:
            df = df[~df["is_total"]]
        meta = ResultMeta(
            shape=self.shape,
            window=self.window,
            empty=int(total["_rows"]) == 0,
            metrics=self.metrics,
            dimensions=self.dimensions,
            trading_days=_int(total["_td"]),
            prior_trading_days=_int(total["_ptd"]),
            grain=self.grain,
            buckets=self.buckets,
            prior_buckets=self.prior_buckets,
            sql=self.sql,
            params=self.params,
            events=self.events,
        )
        df = df.drop(columns=["_lvl", "_rows", "_td", "_ptd"]).reset_index(drop=True)
        return Result(df, meta)


def _int(value) -> int | None:
    return None if pd.isna(value) else int(value)


def compile_query(query: Query, registry: Registry, calendar: Calendar) -> CompiledQuery:
    """Check a query against the registry, resolve its window and write its SQL."""
    return _Compiler(query, registry, calendar).compile()


def run_query(
    con: duckdb.DuckDBPyConnection,
    query: Query,
    registry: Registry,
    calendar: Calendar | None = None,
) -> Result:
    """Compile a query and run it against the models of `con`."""
    return compile_query(query, registry, calendar or Calendar.from_db(con)).run(con)


# --- the compiler -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _FrameBucket:
    ordinal: int
    bucket: Bucket
    lo: date  # the part of the bucket inside its window
    hi: date
    prior_lo: date | None  # the prior bucket with the same ordinal, inside the comparison
    prior_hi: date | None


def _date(d: date | None) -> str:
    return "null::date" if d is None else f"date '{d.isoformat()}'"


def _bin_condition(column: str, b: Bin) -> str:
    if b.high is None:
        return f"{column} >= {b.low}"
    if b.high == b.low:
        return f"{column} = {b.low}"
    return f"{column} between {b.low} and {b.high}"


def _indent(sql: str, spaces: int = 4) -> str:
    return "\n".join(" " * spaces + line if line else line for line in sql.splitlines())


class _Compiler:
    def __init__(self, query: Query, registry: Registry, calendar: Calendar):
        self.q = query
        self.reg = registry
        self.cal = calendar
        self.params: dict = {}
        try:
            self._check()
        except RegistryError as error:
            raise QueryError(str(error)) from None

    # --- checks and plan ----------------------------------------------------------------------

    def _check(self) -> None:
        q, reg = self.q, self.reg
        if q.window is not None and q.window.startswith("$"):
            raise QueryError(f"window: the parameter {q.window} is not bound")
        model = reg.model(q.model)
        if not model.exposed:
            raise QueryError(f"model: {q.model} is not exposed to components (models.yml)")
        if model.time is None and q.window is not None:
            raise QueryError(
                f"window: {q.model} has no time column in models.yml (a snapshot), so no window "
                "applies to it; leave out window"
            )
        if model.time is None and q.compare != "none":
            raise QueryError(f"compare: {q.model} is a snapshot without a history to compare")
        if model.time is not None and q.window is None:
            raise QueryError(
                f"window: {q.model} has the time column {model.time}, so the query needs a window"
            )
        self.model = model
        for field, items in (("metrics", q.metrics), ("dimensions", q.dimensions)):
            if len(set(items)) != len(items):
                raise QueryError(f"{field}: an entry is listed twice")
        dims = {d: reg.dimension(d) for d in q.dimensions}
        for dim in dims.values():
            dim.column_on(q.model)  # fails when the dimension is not on the model
        time = [d for d, dim in dims.items() if dim.domain == "calendar"]
        if len(time) > 1:
            raise QueryError(f"dimensions: at most one time dimension, not {', '.join(time)}")
        self.time: str | None = time[0] if time else None
        self.n_dims = [d for d in q.dimensions if d != self.time]
        self.dims = dims

        self.window: ResolvedWindow | None = None
        if q.window is not None:
            try:
                self.window = resolve(q.window, self.cal, q.compare)
            except ValueError as error:
                raise QueryError(f"window: {error}") from None
        window = self.window
        self.compare_run = bool(window and window.comparison is not None and window.compare_ok)

        fixed = TIME_GRAIN.get(self.time) if self.time else None
        if self.time is None and q.grain:
            raise QueryError(f"grain needs a time dimension ({TIME_BUCKET} or {_time_dims()})")
        if fixed and q.grain and q.grain != fixed:
            raise QueryError(f"grain {q.grain} contradicts the dimension {self.time} ({fixed})")
        self.grain: Grain | None = (
            (fixed or q.grain or self.window.current.grain) if self.time else None
        )
        if q.buckets_before and self.time is None:
            raise QueryError("buckets_before needs a time dimension")
        if q.top and not self.n_dims:
            raise QueryError("top needs a dimension other than the time bucket")
        if q.keep_prior_only:
            if q.compare == "none":
                raise QueryError("keep_prior_only needs a comparison")
            if self.time or q.top:
                raise QueryError("keep_prior_only does not work with a time dimension or top")

        self.bins: dict[str, list[Bin]] = {}
        for dim_id in self.n_dims:
            if self.dims[dim_id].bins:
                self.bins[dim_id] = self.dims[dim_id].bin_list
        for dim_id, items in q.bins.items():
            if dim_id not in self.n_dims:
                raise QueryError(f"bins: {dim_id} is not a dimension of the query")
            if self.dims[dim_id].grain == "day":
                raise QueryError(f"bins: {dim_id} is a calendar dimension and cannot be binned")
            try:
                self.bins[dim_id] = parse_bins(items)
            except ValueError as error:
                raise QueryError(f"bins: {dim_id}: {error}") from None
            # the result's dimension carries the bins the query used, for the transforms
            self.dims[dim_id] = self.dims[dim_id].model_copy(update={"bins": list(items)})

        self._plan_metrics()
        self._plan_filters()
        self._plan_buckets()

    def _plan_metrics(self) -> None:
        q, reg = self.q, self.reg
        counts = [self.model.sample_size] if self.model.sample_size else []
        for metric_id in q.metrics:
            counts += [c for c in reg.min_n(metric_id, q.model) if c not in counts]
        self.counts = counts
        on_model = set(reg.metrics_on(q.model))
        self.sorts = {
            d: self.dims[d].sort if self.dims[d].sort.by in (on_model | {"value"}) else SortSpec()
            for d in self.n_dims
        }
        if q.top:
            sort = [q.top.by]
        else:
            sort = [s.by for s in self.sorts.values() if s.by != "value"]
        self.plan = reg.plan([*q.metrics, *counts, *sort], q.model)
        if q.top and reg.metric(q.top.by).totals:
            raise QueryError(f"top: cannot rank by the relative metric {q.top.by}")

    def _plan_filters(self) -> None:
        """Bind the filter values; `self.where` and `self.focus` hold (dimension, value,
        parameter names)."""
        self.where = self._bind_filter("where", self.q.where)
        self.focus = self._bind_filter("focus", self.q.focus)
        relative = [m for m in self.plan.computed if self.reg.metric(m).totals]
        if self.where and relative and not self.n_dims and not self.focus:
            dim_id = next(iter(self.q.where))
            raise QueryError(
                f"where: {dim_id} narrows the totals to its own rows, so "
                f"{', '.join(relative)} would be those of the whole (100% for a share or "
                f"penetration); use focus: {{ {dim_id}: ... }} to keep the totals of "
                "everything, or group by a dimension to get values within the filter"
            )

    def _bind_filter(
        self, field: str, filters: dict[str, WhereValue]
    ) -> list[tuple[Dimension, WhereValue, list[str]]]:
        bound = []
        for dim_id, value in filters.items():
            dim = self.reg.dimension(dim_id)
            if dim_id == TIME_BUCKET:
                raise QueryError(f"{field}: cannot filter on {TIME_BUCKET}; use the window")
            dim.column_on(self.q.model)
            values = value if isinstance(value, list) else [value]
            if isinstance(value, list) and not value:
                raise QueryError(f"{field}: {dim_id} has an empty list")
            if any(isinstance(v, str) and v.startswith("$") for v in values):
                raise QueryError(f"{field}: {dim_id}: a parameter is not bound")
            names: list[str] = []
            bins = self.bins.get(dim_id) or dim.bin_list
            if bins and value is not None:
                labels = {b.label for b in bins}
                unknown = [v for v in values if str(v) not in labels]
                if unknown:
                    raise QueryError(f"{field}: {dim_id}: {unknown} are not bins of {dim_id}")
            elif value is not None:
                for v in values:
                    names.append(f"{field[0]}{len(self.params)}")
                    self.params[names[-1]] = v
            bound.append((dim, value, names))
        return bound

    def _plan_buckets(self) -> None:
        self.frame_buckets: list[_FrameBucket] = []
        self.prior_buckets: list[Bucket] = []
        self.extra: list[_FrameBucket] = []
        if self.time is None:
            return
        cur, prior = self.window.current, self.window.comparison
        self.prior_buckets = buckets(prior, self.grain) if self.compare_run else []
        for i, b in enumerate(buckets(cur, self.grain)):
            p = self.prior_buckets[i] if i < len(self.prior_buckets) else None
            self.frame_buckets.append(
                _FrameBucket(
                    i,
                    b,
                    max(b.start, cur.start),
                    min(b.end, cur.end),
                    max(p.start, prior.start) if p else None,
                    min(p.end, prior.end) if p else None,
                )
            )
        extra: list[Bucket] = []
        window: Window = cur
        for _ in range(self.q.buckets_before):
            b = bucket_before(window, self.grain)
            if not self.cal.in_history(b.start):
                break
            extra.insert(0, b)
            window = dataclasses.replace(window, start=b.start)
        self.extra = [
            _FrameBucket(i - len(extra), b, b.start, b.end, None, None) for i, b in enumerate(extra)
        ]

    # --- SQL pieces ---------------------------------------------------------------------------

    def _column(self, dim: Dimension, alias: str, model: str | None = None) -> str:
        return f"{alias}.{dim.column_on(model or self.q.model)}"

    def _dim_expr(self, dim_id: str) -> str:
        column = self._column(self.dims[dim_id], "m")
        bins = self.bins.get(dim_id)
        if not bins:
            return column
        whens = " ".join(
            f"when {_bin_condition(column, b)} then {sql_literal(b.label)}" for b in bins
        )
        return f"case {whens} end"

    def _filters(self, alias: str, model: str | None = None, filters=None) -> list[str]:
        """The conditions of `filters` (default: the where) on `alias` (a row of the model, or
        of dim_calendar)."""
        conditions = []
        for dim, value, names in self.where if filters is None else filters:
            if model == "dim_calendar" and dim.grain != "day":
                continue  # a trading day does not depend on the payment type or the item
            column = self._column(dim, alias, model)
            bins = self.bins.get(dim.id) or dim.bin_list
            values = value if isinstance(value, list) else [value]
            if value is None:
                if bins:
                    conds = [_bin_condition(column, b) for b in bins]
                    conditions.append(f"not coalesce({' or '.join(conds)}, false)")
                else:
                    conditions.append(f"{column} is null")
            elif bins:
                chosen = [b for b in bins if b.label in {str(v) for v in values}]
                conditions.append(
                    "(" + " or ".join(_bin_condition(column, b) for b in chosen) + ")"
                )
            elif len(names) == 1:
                conditions.append(f"{column} = ${names[0]}")
            else:
                conditions.append(f"{column} in ({', '.join('$' + n for n in names)})")
        return conditions

    def _aggregate(self, metric_id: str) -> str:
        agg = self.reg.metric(metric_id).aggregates[self.q.model]
        return f"coalesce({agg.sql}, 0)" if agg.func in ADDITIVE_FUNCS else agg.sql

    def _ranking_expr(self, metric_id: str) -> str:
        """A metric as an expression over the aggregates of the rows (for the top N)."""
        metric = self.reg.metric(metric_id)
        if metric.kind in AGGREGATE_KINDS:
            return self._aggregate(metric_id)
        expression = render_expression(
            metric.expression,
            ref=self._ranking_expr,
            total=lambda m: "null",
            trading_days=str(self.window.current.trading_days) if self.window else "null",
        )
        return f"({expression})"

    def _value(self, metric_id: str, side: str, alias: str = "j", in_total: bool = False) -> str:
        """A metric of one row of the frame: a column for an aggregate, else its expression."""
        metric = self.reg.metric(metric_id)
        prefix = "" if side == "cur" else "prior_"
        if metric.kind in AGGREGATE_KINDS:
            return f"{alias}.{prefix}{metric_id}"

        def total(m: str) -> str:
            if in_total:
                raise QueryError(f"metric {metric_id}: total.{m} inside a total is not supported")
            return self._value(m, side, "t", in_total=True)

        expression = render_expression(
            metric.expression,
            ref=lambda m: self._value(m, side, alias, in_total),
            total=total,
            trading_days=f"{alias}.{'_td' if side == 'cur' else '_ptd'}",
        )
        return f"({expression})"

    # --- the statement ------------------------------------------------------------------------

    def compile(self) -> CompiledQuery:
        q = self.q
        if self.window is not None:
            cur, prior = self.window.current, self.window.comparison
            self.params |= {"cur_lo": cur.start, "cur_hi": cur.end}
        if self.compare_run:
            self.params |= {"prior_lo": prior.start, "prior_hi": prior.end}
        if self.extra:
            self.params |= {"extra_lo": self.extra[0].bucket.start}
            self.params |= {"extra_hi": self.extra[-1].bucket.end}

        ctes: list[tuple[str, str]] = []
        if self.time:
            ctes.append(("frame_b", self._frame_buckets()))
            if self.compare_run:
                ctes.append(("prior_b", self._prior_buckets()))
        sides = [
            "cur",
            *(["prior"] if self.compare_run else []),
            *(["extra"] if self.extra else []),
        ]
        for side in sides:
            ctes.append((f"{side}_r", self._rows(side)))
        if q.top:
            ctes.append(("ranked", self._ranked()))
            for side in sides:
                ctes.append((f"{side}_k", self._keyed(side)))
        for side in sides:
            ctes.append((f"{side}_a", self._aggregated(side)))
        if self.extra:
            ctes.append(
                ("cur_all", "select * from cur_a\nunion all by name\nselect * from extra_a")
            )
        ctes += self._frame()
        ctes.append(("jt", self._trading_days()))
        if self.focus and self._needs_total():
            ctes += self._universe(sides)
        ctes.append(("f", self._computed()))
        sql = "with\n" + ",\n".join(f"{name} as (\n{_indent(body)}\n)" for name, body in ctes)
        sql += "\n" + self._final()
        return CompiledQuery(
            sql=sql,
            params=self.params,
            query=q,
            window=self.window,
            metrics=self._metric_specs(),
            dimensions=dict(self.dims),
            shape="series" if self.time else ("rows" if q.dimensions else "single"),
            grain=self.grain,
            buckets=tuple(
                BucketInfo(b.ordinal, b.bucket.start, b.bucket.end)
                for b in [*self.extra, *self.frame_buckets]
            ),
            prior_buckets=tuple(
                BucketInfo(i, b.start, b.end) for i, b in enumerate(self.prior_buckets)
            ),
            events=self._events(),
        )

    def _events(self) -> tuple[DayEvent, ...]:
        """The calendar events of the ranges a query by time shows: the window, the buckets
        before it and the comparison window."""
        if self.time is None:
            return ()
        cur = self.window.current
        ranges = [(self.extra[0].bucket.start if self.extra else cur.start, cur.end)]
        if self.compare_run:
            ranges.append((self.window.comparison.start, self.window.comparison.end))
        days = {e.date: e for lo, hi in ranges for e in self.cal.events_between(lo, hi)}
        return tuple(days[d] for d in sorted(days))

    def _metric_specs(self) -> dict[str, Metric]:
        """The metric of every column, with its sample-size rule on the query's model."""
        specs = {m: self.reg.metric_on(m, self.q.model) for m in self.q.metrics}
        specs |= {f"n_{c}": self.reg.metric_on(c, self.q.model) for c in self.counts}
        return specs

    def _frame_buckets(self) -> str:
        rows = [
            f"({b.ordinal}, {_date(b.bucket.start)}, {_date(b.bucket.end)}, {_date(b.lo)}, "
            f"{_date(b.hi)}, {_date(b.prior_lo)}, {_date(b.prior_hi)})"
            for b in [*self.extra, *self.frame_buckets]
        ]
        columns = "_ord, _t, _te, _lo, _hi, _plo, _phi"
        if not rows:
            return (
                "select null::integer as _ord, null::date as _t, null::date as _te, "
                "null::date as _lo, null::date as _hi, null::date as _plo, null::date as _phi "
                "where false"
            )
        return "select * from (values\n    " + ",\n    ".join(rows) + f"\n) as v({columns})"

    def _prior_buckets(self) -> str:
        rows = [
            f"({i}, {_date(b.start)}, {_date(b.end)})" for i, b in enumerate(self.prior_buckets)
        ]
        if not rows:
            return "select null::integer as _ord, null::date as _t, null::date as _te where false"
        return "select * from (values\n    " + ",\n    ".join(rows) + "\n) as v(_ord, _t, _te)"

    def _rows(self, side: str) -> str:
        select = ["m.*"]
        join = ""
        day = f"m.{self.model.time}" if self.model.time else None
        if self.time:
            select += ["b._ord", "b._t"]
            if side == "prior":
                join = f"\njoin prior_b as b on {day} between b._t and b._te"
            else:
                op = ">=" if side == "cur" else "<"
                join = f"\njoin frame_b as b on {day} between b._t and b._te and b._ord {op} 0"
        select += [f"{self._dim_expr(d)} as _n{i}" for i, d in enumerate(self.n_dims)]
        if not self.q.top:
            select.append("false as _other")
        if self.focus:
            focus = " and ".join(self._filters("m", filters=self.focus))
            select.append(f"coalesce({focus}, false) as _f")
        where = [f"{day} between ${side}_lo and ${side}_hi"] if day else []
        where += self._filters("m")
        sql = f"select {', '.join(select)}\nfrom {self.q.model} as m{join}"
        return sql + (f"\nwhere {' and '.join(where)}" if where else "")

    def _n_keys(self, left: str, right: str) -> list[str]:
        return [f"{left}._n{i} is not distinct from {right}._n{i}" for i in range(len(self.n_dims))]

    def _ranked(self) -> str:
        top = self.q.top
        keys = [f"_n{i}" for i in range(len(self.n_dims))]
        order = [
            f"{self._ranking_expr(top.by)} desc nulls last",
            *(f"{k} nulls last" for k in keys),
        ]
        focus = "\nwhere _f" if self.focus else ""
        return (
            f"select {', '.join(keys)}, row_number() over (order by {', '.join(order)}) as _rank\n"
            f"from cur_r{focus}\ngroup by {', '.join(keys)}"
        )

    def _keyed(self, side: str) -> str:
        """Map the groups outside the top N to "Other (k)"; without `other` the frame drops that
        group later, so that the totals still cover every row."""
        n = self.q.top.n
        on = " and ".join(self._n_keys("r", "k"))
        other = f"'Other (' || ((select count(*) from ranked) - {n})::varchar || ')'"
        replace = [f"case when k._rank <= {n} then r._n0::varchar else {other} end as _n0"]
        replace += [
            f"case when k._rank <= {n} then r._n{i} end as _n{i}"
            for i in range(1, len(self.n_dims))
        ]
        return (
            f"select r.* replace ({', '.join(replace)}), coalesce(k._rank > {n}, true) as _other\n"
            f"from {side}_r as r\nleft join ranked as k on {on}"
        )

    def _aggregated(self, side: str) -> str:
        source = f"{side}_k" if self.q.top else f"{side}_r"
        n_keys = [f"_n{i}" for i in range(len(self.n_dims))]
        keys = (["_ord", "_t"] if self.time else []) + n_keys
        grand = side != "extra"
        if self.time and n_keys:
            sets = [keys, ["_ord", "_t"], *([[]] if grand else [])]
            level = "case when grouping(_t) = 1 then 2 when grouping(_n0) = 1 then 1 else 0 end"
        elif self.time:
            sets = [keys, *([[]] if grand else [])]
            level = "case when grouping(_t) = 1 then 2 else 0 end"
        elif n_keys:
            sets = [keys, []]
            level = "case when grouping(_n0) = 1 then 2 else 0 end"
        else:
            sets, level = [], "2"
        select = [*keys, f"{level} as _lvl", "bool_or(_other) as _other", "count(*) as _rows"]
        select += [f"{self._aggregate(m)} as {m}" for m in self.plan.aggregates]
        sql = "select\n    " + ",\n    ".join(select) + f"\nfrom {source}"
        if self.focus:
            sql += "\nwhere _f"
        if sets:
            sql += "\ngroup by grouping sets (" + ", ".join(f"({', '.join(s)})" for s in sets) + ")"
        return sql

    def _universe(self, sides: list[str]) -> list[tuple[str, str]]:
        """The totals of everything the where keeps, per bucket (level 1) and for the window
        (level 2), which the relative metrics read under a focus instead of the focus totals."""
        ctes = []
        aggregates = [f"{self._aggregate(m)} as {m}" for m in self.plan.aggregates]
        for side in sides:
            if self.time:
                level = "case when grouping(_t) = 1 then 2 else 1 end"
                sets = "(_ord, _t)" + (", ()" if side != "extra" else "")
                select = ["_ord", f"{level} as _lvl", "count(*) as _rows", *aggregates]
                group = f"\ngroup by grouping sets ({sets})"
            else:
                select = ["null::integer as _ord", "2 as _lvl", "count(*) as _rows", *aggregates]
                group = ""
            body = "select\n    " + ",\n    ".join(select) + f"\nfrom {side}_r{group}"
            ctes.append((f"{side}_u", body))
        cur = "cur_u"
        if self.extra:
            ctes.append(
                ("cur_all_u", "select * from cur_u\nunion all by name\nselect * from extra_u")
            )
            cur = "cur_all_u"
        if self.compare_run:
            prior_lo, prior_hi = "$prior_lo", "$prior_hi"
        else:
            prior_lo, prior_hi = "null::date", "null::date"
        frame = (
            f"select 2 as _lvl, null::integer as _ord, {self._cur_bounds()}, "
            f"{prior_lo} as _plo, {prior_hi} as _phi"
        )
        if self.time:
            frame = (
                "select 1 as _lvl, b._ord, b._lo, b._hi, b._plo, b._phi\nfrom frame_b as b\n"
                f"union all\n{frame}"
            )
        on = "c._lvl = f._lvl and c._ord is not distinct from f._ord"
        joins = f"from u_frame as f\nleft join {cur} as c on {on}"
        if self.compare_run:
            joins += f"\nleft join prior_u as p on {on.replace('c.', 'p.')}"
        select = ["f.*", *self._joined_values("f")]
        ctes.append(("u_frame", frame))
        ctes.append(("uj", "select\n    " + ",\n    ".join(select) + "\n" + joins))
        prior = (
            self._trading_days_expr("_plo", "_phi", member=False)
            if self.compare_run
            else "null::bigint"
        )
        ctes.append(
            (
                "ut",
                f"select j.*,\n    {self._trading_days_expr('_lo', '_hi', member=False)} as _td,"
                f"\n    {prior} as _ptd\nfrom uj as j",
            )
        )
        return ctes

    def _cur_bounds(self) -> str:
        """The current window's first and last day as `_lo, _hi` (NULL for a snapshot)."""
        if self.window is None:
            return "null::date as _lo, null::date as _hi"
        return "$cur_lo as _lo, $cur_hi as _hi"

    def _joined_values(self, frame: str) -> list[str]:
        """The aggregates of the current (c) and prior (p) side on a row of the frame."""
        values = ["coalesce(c._rows, 0) as _rows"]
        for m in self.plan.aggregates:
            func = self.reg.metric(m).aggregates[self.q.model].func
            cur = f"coalesce(c.{m}, 0)" if func in ADDITIVE_FUNCS else f"c.{m}"
            values.append(f"{cur} as {m}")
        if self.compare_run:
            for m in self.plan.aggregates:
                func = self.reg.metric(m).aggregates[self.q.model].func
                prior = f"coalesce(p.{m}, 0)" if func in ADDITIVE_FUNCS else f"p.{m}"
                values.append(f"case when {frame}._plo is not null then {prior} end as prior_{m}")
        return values

    def _frame(self) -> list[tuple[str, str]]:
        n_cols = [f"_n{i}" for i in range(len(self.n_dims))]
        n_list = "".join(f"{c}, " for c in n_cols)
        if self.compare_run:
            prior_lo, prior_hi = "$prior_lo", "$prior_hi"
        else:
            prior_lo, prior_hi = "null::date", "null::date"
        drop_other = self.q.top is not None and not self.q.top.other
        if self.time is None:
            keys = f"select _lvl, {n_list}coalesce(_other and _lvl = 0, false) as _other from cur_a"
            if drop_other:
                keys += "\nwhere not coalesce(_other and _lvl = 0, false)"
            if self.q.keep_prior_only and self.compare_run:
                keys += f"\nunion\nselect _lvl, {n_list}false from prior_a"
            frame = (
                f"select k.*, {self._cur_bounds()}, {prior_lo} as _plo, "
                f"{prior_hi} as _phi\nfrom keys as k"
            )
            on_cur = ["c._lvl = f._lvl", *self._n_keys("c", "f")]
            ctes = [("keys", keys), ("frame", frame)]
            source = "cur_a"
        else:
            bucket_cols = "b._ord, b._t, b._lo, b._hi, b._plo, b._phi"
            parts = []
            if n_cols:
                keys = f"select distinct {', '.join(n_cols)}, _other from cur_a where _lvl = 0"
                if drop_other:
                    keys += " and not _other"
                parts.append(
                    f"select 0 as _lvl, {bucket_cols}, k.*\nfrom frame_b as b\ncross join keys as k"
                )
                nulls = "".join(f", null as _n{i}" for i in range(len(n_cols)))
                parts.append(f"select 1, {bucket_cols}{nulls}, false\nfrom frame_b as b")
                tail = f"{nulls}, false"
            else:
                keys = None
                parts.append(f"select 0 as _lvl, {bucket_cols}, false as _other\nfrom frame_b as b")
                tail = ", false"
            parts.append(f"select 2, null, null, $cur_lo, $cur_hi, {prior_lo}, {prior_hi}{tail}")
            frame = "\nunion all\n".join(parts)
            on_cur = [
                "c._lvl = f._lvl",
                "c._ord is not distinct from f._ord",
                *self._n_keys("c", "f"),
            ]
            ctes = ([("keys", keys)] if keys else []) + [("frame", frame)]
            source = "cur_all" if self.extra else "cur_a"
        joins = f"from frame as f\nleft join {source} as c on {' and '.join(on_cur)}"
        if self.compare_run:
            on_prior = [c.replace("c.", "p.", 1) for c in on_cur]
            joins += f"\nleft join prior_a as p on {' and '.join(on_prior)}"
        select = ["f.*", *self._joined_values("f")]
        ctes.append(("j", "select\n    " + ",\n    ".join(select) + "\n" + joins))
        return ctes

    def _trading_days_expr(self, lo: str, hi: str, member: bool = True) -> str:
        """The trading days of a row of `j`; `member=False` for the totals of the universe,
        which neither the focus nor the row's own calendar values restrict. NULL for a model
        to which the calendar does not apply."""
        if not self.model.calendar:
            return "null::bigint"
        conditions = ["c.is_trading_day", f"c.date between j.{lo} and j.{hi}"]
        conditions += self._filters("c", "dim_calendar")
        if not member:
            count = f"(select count(*) from dim_calendar as c where {' and '.join(conditions)})"
            return f"case when j.{lo} is not null then {count} end"
        conditions += self._filters("c", "dim_calendar", self.focus)
        day = [(i, d) for i, d in enumerate(self.n_dims) if self.dims[d].grain == "day"]
        if day:
            same = " and ".join(
                f"c.{self.dims[d].column_on('dim_calendar')} is not distinct from j._n{i}"
                for i, d in day
            )
            conditions.append(f"(j._lvl <> 0 or ({same}))")
        guard = f"j.{lo} is not null"
        if day and self.q.top and self.q.top.other:
            guard += " and not j._other"
        count = f"(select count(*) from dim_calendar as c where {' and '.join(conditions)})"
        return f"case when {guard} then {count} end"

    def _trading_days(self) -> str:
        prior = self._trading_days_expr("_plo", "_phi") if self.compare_run else "null::bigint"
        return (
            f"select j.*,\n    {self._trading_days_expr('_lo', '_hi')} as _td,\n"
            f"    {prior} as _ptd\nfrom j"
        )

    def _needs_total(self) -> bool:
        return any(self.reg.metric(m).totals for m in self.plan.computed)

    def _computed(self) -> str:
        sides = ["cur", *(["prior"] if self.compare_run else [])]
        select = ["j.*"]
        for side in sides:
            prefix = "" if side == "cur" else "prior_"
            select += [f"{self._value(m, side)} as {prefix}{m}" for m in self.plan.computed]
        sql = "select\n    " + ",\n    ".join(select) + "\nfrom jt as j"
        if self._needs_total():
            if self.time is None:
                on = "t._lvl = 2"
            else:
                # the universe has a level-1 row per bucket; without a focus, a series without
                # groups has its bucket totals at level 0
                member_total = 1 if self.n_dims or self.focus else 0
                on = (
                    "t._ord is not distinct from j._ord and "
                    f"t._lvl = case when j._lvl = 2 then 2 else {member_total} end"
                )
            sql += f"\nleft join {'ut' if self.focus else 'jt'} as t on {on}"
        return sql

    def _final(self) -> str:
        q = self.q
        internal = {
            d: ("_t" if d == self.time else f"_n{self.n_dims.index(d)}") for d in q.dimensions
        }
        cols = [f"{internal[d]} as {d}" for d in q.dimensions]
        cols.append("_lvl > 0 as is_total")
        if q.top and q.top.other:
            cols.append("_other as is_other")
        if q.buckets_before:
            cols.append("coalesce(_ord >= 0, true) as in_window")
        cols += list(q.metrics)
        compare = q.compare != "none"
        for m in q.metrics:
            if not compare:
                break
            timestamp = self.reg.metric(m).format == "timestamp"
            if self.compare_run:
                cols.append(f"prior_{m}")
                if not timestamp:
                    cols += [
                        f"{m} / nullif(prior_{m}, 0) - 1 as {m}_change",
                        f"{m} - prior_{m} as {m}_delta",
                    ]
            else:
                null = "null::timestamp" if timestamp else "null::double"
                cols.append(f"{null} as prior_{m}")
                if not timestamp:
                    cols += [f"null::double as {m}_change", f"null::double as {m}_delta"]
        cols += [f"{c} as n_{c}" for c in self.counts]
        if compare:
            cols += [
                f"{'prior_' + c if self.compare_run else 'null::double'} as prior_n_{c}"
                for c in self.counts
            ]
        if q.dimensions:
            cols.append("_td as trading_days")
            if compare:
                cols.append("_ptd as prior_trading_days")
        cols += ["_lvl", "_rows", "_td", "_ptd"]
        return (
            "select\n    " + ",\n    ".join(cols) + "\nfrom f\norder by " + ", ".join(self._order())
        )

    def _order(self) -> list[str]:
        order = ["_lvl = 2"]
        if self.time:
            order += ["_ord", "_lvl"]
        if not self.n_dims:
            return order
        order.append("_other")
        if self.q.top:
            order.append(f"{self.q.top.by} desc nulls last")
        else:
            for i, d in enumerate(self.n_dims):
                sort = self.sorts[d]
                direction = "desc" if sort.desc else "asc"
                values = self.dims[d].values
                if sort.by != "value":
                    order.append(f"{sort.by} {direction} nulls last")
                elif d in self.bins:
                    whens = " ".join(
                        f"when {sql_literal(b.label)} then {k}" for k, b in enumerate(self.bins[d])
                    )
                    order.append(f"case _n{i} {whens} end {direction} nulls last")
                elif values:
                    whens = " ".join(
                        f"when {sql_literal(v)} then {k}" for k, v in enumerate(values)
                    )
                    order.append(f"case _n{i} {whens} end {direction} nulls last")
                else:
                    order.append(f"_n{i} {direction} nulls last")
        order += [f"_n{i} nulls last" for i in range(len(self.n_dims))]
        return order


def _time_dims() -> str:
    return ", ".join(GRAIN_COLUMN.values())
