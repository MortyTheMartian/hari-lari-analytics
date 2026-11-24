"""`classify`: add a class column from thresholds, quadrants or the item lifecycle."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from hari_lari_analytics.reporting.result import Classification, Result
from hari_lari_analytics.reporting.transforms import (
    Options,
    Requirements,
    TransformContext,
    TransformError,
    need_columns,
    transform,
)
from hari_lari_analytics.reporting.transforms.quality import small_n_mask
from hari_lari_analytics.reporting.transforms.shape import row_masks

QUADRANTS = ("high_high", "high_low", "low_high", "low_low")  # the x level first
LIFECYCLE = ("new", "rising", "steady", "fading")


class Band(Options):
    upto: float  # the band holds the values up to and including this one
    label: str


class ClassifyOptions(Options):
    """`mode: thresholds` needs `by`, `bands` and `otherwise`; `quadrants` needs `x` and `y`;
    `lifecycle` needs `by`. Options of another mode are refused."""

    mode: Literal["thresholds", "quadrants", "lifecycle"]
    into: str = "class"
    by: str | None = None
    # thresholds: the first band whose `upto` is at or above the value wins, else `otherwise`
    bands: list[Band] = []
    otherwise: str | None = None
    # quadrants: each axis splits at the median or mean of the rows, or at a given number
    x: str | None = None
    y: str | None = None
    x_split: float | Literal["median", "mean"] = "median"
    y_split: float | Literal["median", "mean"] = "median"
    labels: dict[Literal["high_high", "high_low", "low_high", "low_low"], str] = {}
    # lifecycle: new (no sales in the prior period), rising, steady or fading by the change of `by`
    rising_at: float = 0.25
    fading_at: float = -0.25
    # leave rising, steady and fading empty when both periods have fewer than this many orders
    # with the item (the item's orders in that period, not its lifetime); None: no rule
    min_n: int | None = Field(default=15, ge=1)

    @model_validator(mode="after")
    def _per_mode(self):
        needs = {
            "thresholds": {"by", "bands", "otherwise"},
            "quadrants": {"x", "y"},
            "lifecycle": {"by"},
        }[self.mode]
        given = {
            "by": self.by is not None,
            "bands": bool(self.bands),
            "otherwise": self.otherwise is not None,
            "x": self.x is not None,
            "y": self.y is not None,
        }
        missing = sorted(n for n in needs if not given[n])
        if missing:
            raise ValueError(f"mode {self.mode} needs {', '.join(missing)}")
        foreign = sorted(n for n, has in given.items() if has and n not in needs)
        if foreign:
            raise ValueError(f"{', '.join(foreign)} do not belong to mode {self.mode}")
        if self.mode == "thresholds":
            uppers = [b.upto for b in self.bands]
            if uppers != sorted(set(uppers)):
                raise ValueError("bands must be in ascending order of upto, without repeats")
        if self.mode == "lifecycle" and not self.fading_at < self.rising_at:
            raise ValueError("fading_at must be below rising_at")
        return self


def _requires(options: ClassifyOptions) -> Requirements:
    if options.mode == "lifecycle":
        return Requirements(compare=True, metrics=(options.by,))
    return Requirements()


@transform("classify", ClassifyOptions, shapes=("rows",), requires=_requires)
def classify(result: Result, options: ClassifyOptions, ctx: TransformContext) -> Result:
    """Add a class column: thresholds on a column (ABC classes), quadrants of two columns against
    their medians (menu engineering) or the lifecycle of an item (new, rising, steady, fading).

    Only the regular rows get a class; `Other` and total rows, and rows whose value is unknown,
    get none. The classes and the split values used are kept in `meta.classifications`."""
    if options.into in result.df.columns:
        raise TransformError(f"transform classify: the column {options.into} already exists")
    run = {"thresholds": _thresholds, "quadrants": _quadrants, "lifecycle": _lifecycle}
    classes, info = run[options.mode](result, options)
    df = result.df.copy()
    df[options.into] = pd.Series(classes, index=df.index, dtype=object)
    meta = {**result.meta.classifications}
    meta[options.into] = Classification(options.into, options.mode, info.pop("classes"), info)
    return result.replace(df, classifications=meta)


def _blank(result: Result) -> pd.Series:
    return pd.Series(None, index=result.df.index, dtype=object)


def _thresholds(result: Result, o: ClassifyOptions):
    need_columns(result, "classify", o.by)
    regular, _, _ = row_masks(result.df)
    values = result.df[o.by]
    out = _blank(result)
    for i in result.df.index[regular & values.notna()]:
        v = values[i]
        out[i] = next((b.label for b in o.bands if v <= b.upto), o.otherwise)
    classes = (*(b.label for b in o.bands), o.otherwise)
    return out, {"classes": classes, **{b.label: b.upto for b in o.bands}}


def _split(values: pd.Series, how: float | str) -> float:
    if how == "median":
        return float(values.median())
    if how == "mean":
        return float(values.mean())
    return float(how)


def _quadrants(result: Result, o: ClassifyOptions):
    need_columns(result, "classify", o.x, o.y)
    df = result.df
    regular, _, _ = row_masks(df)
    known = regular & df[o.x].notna() & df[o.y].notna()
    out = _blank(result)
    if not known.any():
        return out, {"classes": tuple(o.labels.get(q, q) for q in QUADRANTS)}
    x_at, y_at = _split(df.loc[known, o.x], o.x_split), _split(df.loc[known, o.y], o.y_split)
    names = {q: o.labels.get(q, q) for q in QUADRANTS}
    for i in df.index[known]:
        level = ("high" if df.at[i, o.x] >= x_at else "low") + (
            "_high" if df.at[i, o.y] >= y_at else "_low"
        )
        out[i] = names[level]
    return out, {"classes": tuple(names.values()), "x": x_at, "y": y_at}


def _lifecycle(result: Result, o: ClassifyOptions):
    prior = f"prior_{o.by}"
    need_columns(result, "classify", o.by, prior)
    df = result.df
    regular, _, _ = row_masks(df)
    now, before = df[o.by], df[prior]
    known = regular & now.notna() & before.notna() & ((now > 0) | (before > 0))
    out = _blank(result)
    # the sample is small only if both periods are: a new or vanished item is judged by one side
    small = (
        small_n_mask(result, o.by, threshold=o.min_n)
        & small_n_mask(result, o.by, prior=True, threshold=o.min_n)
        if o.min_n is not None
        else pd.Series(False, index=df.index)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        change = now / before - 1
    for i in df.index[known]:
        if before[i] == 0:
            out[i] = "new"  # a fact, however few sales
        elif small[i]:
            continue
        elif change[i] >= o.rising_at:
            out[i] = "rising"
        elif change[i] <= o.fading_at:
            out[i] = "fading"
        else:
            out[i] = "steady"
    return out, {"classes": LIFECYCLE, "rising_at": o.rising_at, "fading_at": o.fading_at}
