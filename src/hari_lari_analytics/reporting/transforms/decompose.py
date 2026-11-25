"""`decompose`: split the change of a KPI over the drivers of one identity of the KPI tree.

The effects add up to the change of the KPI exactly:

- `product` (Y = X1 × X2 × ...): the log-mean (LMDI) effect of driver i is
  L(Y1, Y0) × ln(Xi1 / Xi0), with L(a, b) = (a − b) / (ln a − ln b) and L(a, a) = a. It needs
  every value positive; otherwise the change is not split (status `undefined`).
- `sum` (Y = ± X1 ± X2 ...): the effect of a term is its signed difference.
- `sum_over` (Y = Σ X over the values of a dimension): one row per member with the change of
  its slice; a member without a slice in the prior period is `new`, one without a slice now is
  `gone`, and both are shown whole. With `depth: 2` every other member is split further by the
  identity's `then`, in rows of level 2 below it.

The result (shape `decomposition`) has one row per driver or member, then the KPI as the total
row: `level` (1, or 2 below a member; 0 for the KPI), `metric` (the metric of the values),
`label`, `sign`, `status`, `prior`, `current`, `delta`, `change`, `effect` (in the unit of the
KPI), `share_of_change` (effect over the change of the KPI), `is_total`, and the sample-size
columns of the row the values come from. For a `sum_over` the member is in the column named by
the dimension.
"""

from __future__ import annotations

import math
from typing import Literal

import pandas as pd
from pydantic import Field

from hari_lari_analytics.reporting.registry import Identity, Registry, RegistryError
from hari_lari_analytics.reporting.result import DecompositionSpec, Result
from hari_lari_analytics.reporting.transforms import (
    Options,
    Requirements,
    TransformContext,
    TransformError,
    transform,
)
from hari_lari_analytics.reporting.transforms.shape import row_masks

TOLERANCE = 1e-6  # relative: the effects must add up to the change of the KPI


class DecomposeOptions(Options):
    identity: str  # an identity of kpi_tree.yml
    depth: int = Field(default=1, ge=1, le=2)  # 2: split each member of a sum_over by `then`
    # the members of a sum_over: largest effect first, or in the order of the query
    order: Literal["effect", "query"] = "effect"


def _identity(registry: Registry, options: DecomposeOptions) -> tuple[Identity, Identity | None]:
    try:
        identity = registry.identity(options.identity)
    except RegistryError as error:
        raise TransformError(f"transform decompose: {error}") from None
    if options.depth == 1:
        return identity, None
    if identity.then is None:
        raise TransformError(
            f"transform decompose: depth 2 needs an identity with `then`; {identity.id} has none"
        )
    return identity, registry.identity(identity.then)


def _requires(options: DecomposeOptions, registry: Registry) -> Requirements:
    identity, then = _identity(registry, options)
    metrics = [*identity.metrics, *(then.metrics if then else [])]
    if identity.kind != "sum_over":
        return Requirements(metrics=tuple(metrics), compare=True, only_dimensions=())
    return Requirements(
        metrics=tuple(dict.fromkeys(metrics)),
        compare=True,
        only_dimensions=(identity.dimension,),
        totals=True,
        all_groups=True,
    )


def log_mean(a: float, b: float) -> float:
    """The logarithmic mean L(a, b) of two positive numbers; L(a, a) = a."""
    if a == b:
        return a
    return (a - b) / (math.log(a) - math.log(b))


def _value(row: pd.Series, column: str) -> float | None:
    value = row.get(column)
    return None if value is None or pd.isna(value) else float(value)


def split(identity: Identity, row: pd.Series) -> list[float] | None:
    """The effects of the terms of a product or sum identity on the change of its KPI, from the
    current and `prior_` values of one row; None when the split is undefined."""
    values = [(_value(row, m), _value(row, f"prior_{m}")) for _, m in identity.terms]
    y1, y0 = _value(row, identity.kpi), _value(row, f"prior_{identity.kpi}")
    if y1 is None or y0 is None or any(x is None for pair in values for x in pair):
        return None
    if identity.kind == "sum":
        signs = [sign for sign, _ in identity.terms]
        return [sign * (x1 - x0) for sign, (x1, x0) in zip(signs, values, strict=True)]
    if min(y1, y0, *(x for pair in values for x in pair)) <= 0:
        return None
    weight = log_mean(y1, y0)
    return [weight * math.log(x1 / x0) for x1, x0 in values]


def _adds_up(parts: list[float], change: float) -> bool:
    return math.isclose(sum(parts), change, rel_tol=TOLERANCE, abs_tol=TOLERANCE)


def _n_columns(result: Result) -> list[str]:
    return [c for c in result.df.columns if c.startswith(("n_", "prior_n_"))]


@transform(
    "decompose",
    DecomposeOptions,
    shapes=("single", "rows"),
    produces="decomposition",
    requires=_requires,
)
def decompose(result: Result, options: DecomposeOptions, ctx: TransformContext) -> Result:
    """Split the change of a KPI over the drivers of one KPI-tree identity, so that the effects
    add up to the change exactly: log-mean (LMDI) weights for a product, differences for a sum,
    one row per member for a sum over a dimension (new and gone members whole), and with
    `depth: 2` each member split once more by the identity's `then`."""
    identity, then = _identity(ctx.registry, options)
    meta = result.meta
    if not meta.has_comparison:
        raise TransformError("transform decompose needs a comparison: set `compare` on the query")
    expected = "rows" if identity.kind == "sum_over" else "single"
    if meta.shape != expected:
        grouping = f"by {identity.dimension}" if identity.dimension else "without dimensions"
        raise TransformError(
            f"transform decompose: {identity.id} needs a query grouped {grouping} "
            f"(a result of shape {expected}, not {meta.shape})"
        )
    total = result.total
    if total is None:
        raise TransformError("transform decompose: the result has no total row (totals: true)")
    n_cols = _n_columns(result)
    builder = _Rows(result, n_cols)
    kpi_change = _delta(total, identity.kpi)

    if identity.kind == "sum_over":
        _members(builder, result, identity, then, options, kpi_change)
    else:
        effects = split(identity, total)
        if effects is not None and not _adds_up(effects, kpi_change):
            raise TransformError(
                f"transform decompose: the identity {identity.id} does not hold on the result"
            )
        for i, (sign, metric) in enumerate(identity.terms):
            builder.add(
                total,
                metric,
                level=1,
                sign=sign,
                effect=None if effects is None else effects[i],
                status="undefined" if effects is None and kpi_change is not None else None,
            )
    builder.add(total, identity.kpi, level=0, effect=kpi_change, is_total=True)

    df = builder.frame(kpi_change, identity.dimension)
    spec = DecompositionSpec(
        identity=identity.id,
        kind=identity.kind,
        kpi=identity.kpi,
        depth=options.depth,
        dimension=identity.dimension,
        then=then.id if then else None,
    )
    return result.replace(df, shape="decomposition", decomposition=spec)


def _delta(row: pd.Series, metric: str) -> float | None:
    current, prior = _value(row, metric), _value(row, f"prior_{metric}")
    return None if current is None or prior is None else current - prior


def _members(
    builder: _Rows,
    result: Result,
    identity: Identity,
    then: Identity | None,
    options: DecomposeOptions,
    kpi_change: float | None,
) -> None:
    dimension, driver = identity.dimension, identity.driver
    regular, other, _ = row_masks(result.df)
    members = result.df[regular | other].copy()
    members["_effect"] = pd.Series(
        [_delta(row, driver) for _, row in members.iterrows()], index=members.index, dtype=float
    )
    if options.order == "effect":  # the largest effects first, "Other (k)" last
        members = members.assign(
            _other=members["is_other"].astype(bool) if "is_other" in members else False,
            _size=members["_effect"].abs(),
        ).sort_values(["_other", "_size"], ascending=[True, False], kind="stable")
    effects = members["_effect"].dropna().tolist()
    complete = kpi_change is not None and len(effects) == len(members)
    if complete and not _adds_up(effects, kpi_change):
        raise TransformError(
            f"transform decompose: the members of {dimension} do not add up to {identity.kpi}; "
            "every group of both windows must be in the result"
        )
    dim = result.meta.dimensions[dimension]
    for _, row in members.iterrows():
        effect = None if pd.isna(row["_effect"]) else float(row["_effect"])
        current, prior = _value(row, driver), _value(row, f"prior_{driver}")
        status = None
        if effect is not None and prior == 0 and current != 0:
            status = "new"
        elif effect is not None and current == 0 and prior != 0:
            status = "gone"
        parts = None
        if then is not None and status is None and effect is not None and effect != 0:
            parts = split(then, row)
            status = "undefined" if parts is None else None
        member = row[dimension]
        builder.add(
            row, driver, level=1, effect=effect, status=status, member=member,
            label=dim.value_label(member),
        )  # fmt: skip
        if parts is None:
            continue
        if not _adds_up(parts, effect):
            raise TransformError(
                f"transform decompose: the identity {then.id} does not hold for "
                f"{dim.value_label(member)}"
            )
        for (sign, metric), part in zip(then.terms, parts, strict=True):
            builder.add(row, metric, level=2, sign=sign, effect=part, member=member)


class _Rows:
    def __init__(self, result: Result, n_cols: list[str]):
        self.result = result
        self.n_cols = n_cols
        self.items: list[dict] = []

    def add(
        self,
        row: pd.Series,
        metric: str,
        level: int,
        effect: float | None,
        sign: int = 1,
        status: str | None = None,
        is_total: bool = False,
        member=None,
        label: str | None = None,
    ) -> None:
        current, prior = _value(row, metric), _value(row, f"prior_{metric}")
        spec = self.result.meta.metrics[metric]
        item = {
            "_member": member,
            "level": level,
            "metric": metric,
            "label": label or spec.label,
            "sign": sign,
            "status": status,
            "prior": prior,
            "current": current,
            "delta": None if current is None or prior is None else current - prior,
            "change": None if current is None or not prior else current / prior - 1,
            "effect": effect,
            "is_total": is_total,
        }
        if "is_other" in self.result.df:
            item["is_other"] = bool(row.get("is_other", False)) and not is_total
        item |= {c: row.get(c) for c in self.n_cols}
        self.items.append(item)

    def frame(self, kpi_change: float | None, dimension: str | None) -> pd.DataFrame:
        df = pd.DataFrame(self.items)
        for column in ("prior", "current", "delta", "change", "effect"):
            df[column] = df[column].astype(float)
        share = df["effect"] / kpi_change if kpi_change else float("nan")
        df.insert(df.columns.get_loc("effect") + 1, "share_of_change", share)
        if dimension is None:
            return df.drop(columns="_member")
        return df.rename(columns={"_member": dimension})
