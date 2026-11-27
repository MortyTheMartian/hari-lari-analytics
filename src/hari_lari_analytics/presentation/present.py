"""The presentation step: a result as display cells, the same for every renderer.

`present(result)` gives one `Row` per row of the result, each with a `Cell` per column: the raw
value, its display string (`formats.py`), the tone of a change (good, bad or neutral), whether
the value is below its sample-size rule (`small`) and the count of that rule (`n`). The
matplotlib visuals draw from these cells and the JSON bundle (plan 8.9) writes them, so a
frontend never formats a number or applies a rule (docs/components.md, "Visuals", D16).

The rules: a value is small by the counts of its own period (the metric's `min_n`, as
`small_n_mask`); a change, delta or growth is small when either side is; a comparison column
is `n/a` when the comparison is not valid. Matrix cells carry no sample size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd

from hari_lari_analytics.analysis.result import Result
from hari_lari_analytics.analysis.transforms.quality import small_n_mask
from hari_lari_analytics.presentation import formats
from hari_lari_analytics.presentation.formats import NA, Style, Tone, is_missing
from hari_lari_analytics.semantic.registry import TIME_DIMENSIONS, Format, Metric

ColumnKind = Literal["dimension", "value", "prior", "change", "delta", "growth", "n", "other"]
RowKind = Literal["row", "total", "other"]
TOTAL_LABEL = "Total"


@dataclass(frozen=True)
class Cell:
    value: Any  # the raw value as a plain Python value; None when missing
    text: str
    tone: Tone | None = None  # changes, deltas and growth only
    small: bool = False  # below the sample-size rule
    n: int | None = None  # the smallest count of the rule, for "n=12"


@dataclass(frozen=True)
class Column:
    id: str  # the column of the result
    kind: ColumnKind
    label: str
    metric: str | None = None  # the metric of a value, prior, change, delta, growth or n column
    format: Format | None = None
    unit: str = ""


@dataclass(frozen=True)
class Row:
    position: int  # the row's position in the result
    label: str
    kind: RowKind
    cells: dict[str, Cell]
    small: bool = False  # the row's first metric is below its rule
    n: int | None = None

    def __getitem__(self, column: str) -> Cell:
        return self.cells[column]


@dataclass(frozen=True)
class Presentation:
    columns: dict[str, Column]
    rows: list[Row]
    compare_ok: bool
    has_comparison: bool
    metrics: list[str] = field(default_factory=list)  # the value metrics, in result order

    @property
    def regular(self) -> list[Row]:
        """The rows without the total rows (with "Other")."""
        return [r for r in self.rows if r.kind != "total"]

    @property
    def total(self) -> Row | None:
        """The total of the whole result; on a series, not a bucket's total."""
        totals = [r for r in self.rows if r.kind == "total"]
        whole = [r for r in totals if r.label == TOTAL_LABEL]
        return (whole or totals or [None])[-1]


def plain(value: Any) -> Any:
    """A value of a result as a plain Python value (None when missing)."""
    if is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date() if value == value.normalize() else value.to_pydatetime()
    if isinstance(value, datetime | date | str | bool):
        return value
    return value.item() if hasattr(value, "item") else value


def _bool(series: pd.Series | None, i: int) -> bool:
    return bool(series.iloc[i]) if series is not None and not is_missing(series.iloc[i]) else False


def _n(result: Result, metric: Metric, i: int, prior: bool = False) -> int | None:
    counts = [
        result.df[f"{'prior_' if prior else ''}n_{count}"].iloc[i]
        for count in (metric.min_n or {})
        if f"{'prior_' if prior else ''}n_{count}" in result.df
    ]
    counts = [int(c) for c in counts if not is_missing(c)]
    return min(counts) if counts else None


def dimension_label(result: Result, dim_id: str, value: Any) -> str:
    """The display name of a dimension value: its label, a bucket or a day."""
    dim = result.meta.dimensions[dim_id]
    if dim_id in TIME_DIMENSIONS and not is_missing(value):
        day = pd.Timestamp(value).date()
        grain = result.meta.grain or {"sale_date": "day", "sale_week": "week"}.get(dim_id, "month")
        bucket = next((b for b in result.meta.buckets if b.start == day), None)
        return formats.bucket_label(day, bucket.end if bucket else day, grain)
    return dim.value_label(plain(value) if not isinstance(value, str) else value)


class _Presenter:
    def __init__(self, result: Result, style: Style):
        self.r, self.style = result, style
        meta = result.meta
        self.df = result.df
        self.metrics = meta.value_metrics
        self.compare_ok = meta.compare_ok
        self.small: dict[str, pd.Series] = {}
        self.prior_small: dict[str, pd.Series] = {}
        for m, spec in self.metrics.items():
            self.small[m] = small_n_mask(result, m)
            if all(f"prior_n_{count}" in self.df for count in spec.min_n or {}):
                self.prior_small[m] = small_n_mask(result, m, prior=True)

    def columns(self) -> dict[str, Column]:
        meta = self.r.meta
        cols: dict[str, Column] = {}
        for c in self.df.columns:
            if c in meta.dimensions:
                cols[c] = Column(c, "dimension", meta.dimensions[c].label)
                continue
            kind, metric = self._kind(c)
            if metric is None:
                cols[c] = Column(c, "other", c)
                continue
            spec = meta.metrics[metric] if kind != "n" else meta.metrics[c.removeprefix("prior_")]
            fmt = "count" if kind == "n" else spec.format
            label = {"prior": f"Prior {spec.label.lower()}", "n": f"n {spec.label.lower()}"}.get(
                kind, spec.label
            )
            cols[c] = Column(c, kind, label, metric, fmt, formats.unit(fmt) if kind != "n" else "")
        return cols

    def _kind(self, column: str) -> tuple[ColumnKind, str | None]:
        if column in self.metrics:
            return "value", column
        if column.startswith(("n_", "prior_n_")) and column.removeprefix("prior_") in (
            self.r.meta.n_columns
        ):
            return "n", self.r.meta.metrics[column.removeprefix("prior_")].id
        if column.startswith("prior_") and column.removeprefix("prior_") in self.metrics:
            return "prior", column.removeprefix("prior_")
        for suffix in ("change", "delta", "growth"):
            base = column.removesuffix(f"_{suffix}")
            if base != column and base in self.metrics:
                return suffix, base  # type: ignore[return-value]
        return "other", None

    def cell(self, column: Column, i: int) -> Cell:
        value = self.df[column.id].iloc[i]
        raw = plain(value)
        if column.kind == "dimension":
            return Cell(raw, dimension_label(self.r, column.id, value))
        if column.kind == "other":
            return Cell(raw, "" if raw is None else str(raw))
        metric = self.r.meta.metrics[column.metric] if column.kind != "n" else None
        if column.kind == "n":
            return Cell(raw, formats.value_text(raw, "count"))
        small = bool(self.small[column.metric].iloc[i])
        prior_series = self.prior_small.get(column.metric)
        prior_small = bool(prior_series.iloc[i]) if prior_series is not None else False
        if column.kind == "value":
            return Cell(
                raw,
                formats.value_text(raw, metric.format, self.style),
                small=small,
                n=_n(self.r, metric, i),
            )
        if column.kind == "prior":
            text = formats.value_text(raw, metric.format, self.style) if self.compare_ok else NA
            return Cell(raw, text, small=prior_small, n=_n(self.r, metric, i, prior=True))
        kind = column.kind
        if kind in ("change", "delta") and not self.compare_ok:
            return Cell(raw, NA)
        text = formats.comparison_text(raw, metric, kind, self.style)
        small = small or (prior_small if kind != "growth" else False)
        return Cell(raw, text, formats.tone(raw, metric.up_is_good), small, _n(self.r, metric, i))

    def row(self, i: int, columns: dict[str, Column]) -> Row:
        df = self.df
        kind: RowKind = "row"
        if _bool(df.get("is_total"), i):
            kind = "total"
        elif _bool(df.get("is_other"), i):
            kind = "other"
        cells = {c: self.cell(col, i) for c, col in columns.items()}
        first = next(iter(self.metrics), None)
        dims = [c for c in columns if columns[c].kind == "dimension"]
        if kind == "total" and all(is_missing(df[d].iloc[i]) for d in dims):
            label = TOTAL_LABEL
        else:
            label = " · ".join(cells[d].text for d in dims if not is_missing(df[d].iloc[i]))
            if not label and kind == "total":
                label = TOTAL_LABEL
        return Row(
            i,
            label,
            kind,
            cells,
            small=bool(self.small[first].iloc[i]) if first else False,
            n=_n(self.r, self.metrics[first], i) if first else None,
        )


def present(result: Result, style: Style = "full") -> Presentation:
    """The display cells of a result (see the module docstring)."""
    shape = result.meta.shape
    if shape == "matrix":
        return _matrix(result, style)
    if shape == "decomposition":
        return _decomposition(result, style)
    p = _Presenter(result, style)
    columns = p.columns()
    return Presentation(
        columns=columns,
        rows=[p.row(i, columns) for i in range(len(result.df))],
        compare_ok=result.meta.compare_ok,
        has_comparison=result.meta.has_comparison,
        metrics=list(p.metrics),
    )


def _matrix(result: Result, style: Style) -> Presentation:
    spec = result.meta.matrix
    metric = result.meta.metrics[spec.value.removesuffix("_change").removesuffix("_delta")]
    fmt = metric.format
    is_change = spec.value != metric.id
    row_dim = result.meta.dimensions[spec.rows]
    columns = {spec.rows: Column(spec.rows, "dimension", row_dim.label)}
    col_dim = result.meta.dimensions[spec.columns]
    for v in spec.column_values:
        columns[v] = Column(v, "value", col_dim.value_label(plain(v)), metric.id, fmt)
    rows = []
    for i in range(len(result.df)):
        key = result.df[spec.rows].iloc[i]
        cells = {spec.rows: Cell(plain(key), dimension_label(result, spec.rows, key))}
        for v in spec.column_values:
            raw = plain(result.df[v].iloc[i])
            if is_change:
                text = formats.comparison_text(raw, metric, spec.value.rsplit("_", 1)[1], style)
                cells[v] = Cell(raw, text, formats.tone(raw, metric.up_is_good))
            else:
                cells[v] = Cell(raw, formats.value_text(raw, fmt, style))
        rows.append(Row(i, cells[spec.rows].text, "row", cells))
    return Presentation(
        columns, rows, result.meta.compare_ok, result.meta.has_comparison, [metric.id]
    )


_DECOMPOSITION_VALUES = ("prior", "current", "delta", "change", "effect", "share_of_change")


def _decomposition(result: Result, style: Style) -> Presentation:
    """Each row's values in the format of its own metric, its effect in the KPI's format."""
    spec = result.meta.decomposition
    reg = result.meta.metrics
    kpi = reg[spec.kpi]
    df = result.df
    unit = formats.unit(kpi.format)
    columns = {c: Column(c, "other", c) for c in df.columns}
    columns.update(
        {
            "prior": Column("prior", "prior", "Prior"),
            "current": Column("current", "value", "Current"),
            "delta": Column("delta", "delta", "Δ"),
            "change": Column("change", "change", "Change"),
            "effect": Column("effect", "value", "Effect", kpi.id, kpi.format, unit),
            "share_of_change": Column("share_of_change", "value", "Share", format="pct"),
        }
    )
    if spec.dimension:
        columns[spec.dimension] = Column(
            spec.dimension, "dimension", result.meta.dimensions[spec.dimension].label
        )
    counts = [c for c in df.columns if c.startswith("n_")]
    prior_counts = [c for c in df.columns if c.startswith("prior_n_")]

    def smallest(i: int, names: list[str]) -> int | None:
        values = [int(df[c].iloc[i]) for c in names if not is_missing(df[c].iloc[i])]
        return min(values) if values else None

    rows = []
    ok = result.meta.compare_ok
    for i in range(len(df)):
        row_metric = reg.get(df["metric"].iloc[i]) or kpi
        rule = row_metric.min_n or kpi.min_n or {}
        need = min(rule.values()) if rule else 0
        n, prior_n = smallest(i, counts), smallest(i, prior_counts)
        small = n is not None and n < need
        small_prior = prior_n is not None and prior_n < need
        cells = {}
        for c in df.columns:
            raw = plain(df[c].iloc[i])
            cells[c] = Cell(raw, "" if raw is None else str(raw))
        get = {c: plain(df[c].iloc[i]) for c in _DECOMPOSITION_VALUES}
        fmt = row_metric.format
        cells["current"] = Cell(
            get["current"], formats.value_text(get["current"], fmt, style), small=small, n=n
        )
        prior_text = formats.value_text(get["prior"], fmt, style) if ok else NA
        cells["prior"] = Cell(get["prior"], prior_text, small=small_prior, n=prior_n)
        both = small or small_prior
        cells["delta"] = Cell(
            get["delta"],
            formats.delta_text(get["delta"], fmt, style) if ok else NA,
            formats.tone(get["delta"], row_metric.up_is_good),
            both,
        )
        cells["change"] = Cell(
            get["change"],
            formats.ratio_text(get["change"]) if ok else NA,
            formats.tone(get["change"], row_metric.up_is_good),
            both,
        )
        cells["effect"] = Cell(
            get["effect"],
            formats.delta_text(get["effect"], kpi.format, style) if ok else NA,
            formats.tone(get["effect"], kpi.up_is_good),
            both,
        )
        cells["share_of_change"] = Cell(
            get["share_of_change"], formats.value_text(get["share_of_change"], "pct")
        )
        if spec.dimension:
            cells[spec.dimension] = Cell(
                plain(df[spec.dimension].iloc[i]),
                dimension_label(result, spec.dimension, df[spec.dimension].iloc[i]),
            )
        total = _bool(df.get("is_total"), i)
        other = _bool(df.get("is_other"), i)
        kind: RowKind = "total" if total else "other" if other else "row"
        rows.append(Row(i, str(df["label"].iloc[i]), kind, cells, small=small, n=n))
    return Presentation(columns, rows, ok, result.meta.has_comparison, [kpi.id])
