"""The results of the pandas pipeline on the fixture, frozen as CSV files by `make_golden.py`
(plan t5): the expected `fct_sales_lines` of each case, the report periods, and the numbers
of the cover, the periods, the categories and the payment types.

Values are stored as text: an empty cell is NULL, booleans are `true` and `false`, dates and
timestamps are ISO timestamps, whole numbers have no decimals and other floats are `repr`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

GOLDEN = Path(__file__).parent
PERIOD_ANCHORS = [
    date(2026, 10, 13),  # the fixture's last sale
    date(2026, 10, 25),
    date(2026, 10, 31),
    date(2026, 11, 22),  # the real run
    date(2027, 1, 15),
]
_TEXT_COLUMNS = {"scope", "adjusted_category", "payment_type"}
_BOOL_COLUMNS = {"compare_ok", "empty"}


def text(value) -> str:
    """The golden form of one value."""
    if value is None or (not isinstance(value, str) and np.ndim(value) == 0 and pd.isna(value)):
        return ""
    if isinstance(value, bool | np.bool_):
        return "true" if value else "false"
    if isinstance(value, datetime | date | np.datetime64):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, int | np.integer):
        return str(int(value))
    if isinstance(value, float | np.floating | Decimal):
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    if value == "":
        raise ValueError("an empty string cannot be told from NULL in a golden file")
    return str(value)


def as_text(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """`columns` of `df` in their golden form, rows sorted by all of them."""
    columns = list(df.columns) if columns is None else columns
    out = pd.DataFrame({c: [text(v) for v in df[c]] for c in columns}, columns=columns)
    return out.sort_values(columns).reset_index(drop=True)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def lines(case: str = "fixture") -> pd.DataFrame:
    """The expected `fct_sales_lines` of a case, as text (see `make_golden.LINE_CASES`)."""
    return _read(GOLDEN / "lines" / f"{case}.csv")


def assert_lines(actual: pd.DataFrame, case: str = "fixture", columns: list[str] | None = None):
    """`actual` (the rows of `fct_sales_lines`) has the golden lines of `case`, in `columns`
    (default: every column of the golden file)."""
    expected = lines(case)
    columns = list(expected.columns) if columns is None else columns
    missing = set(columns) - set(actual.columns)
    assert not missing, f"fct_sales_lines lacks {sorted(missing)}"
    expected = expected[columns].sort_values(columns).reset_index(drop=True)
    pd.testing.assert_frame_equal(as_text(actual, columns), expected)


@dataclass(frozen=True)
class Period:
    """A report period (L14D, L6W, L3M) of an anchor, as `periods.report_periods` gave it."""

    key: str
    title: str
    label: str
    prior_label: str
    start: date
    end: date
    prior_start: date
    prior_end: date

    def mask(self, ts: pd.Series) -> pd.Series:
        return (ts >= pd.Timestamp(self.start)) & (ts < pd.Timestamp(self.end + timedelta(days=1)))


def periods(anchor: date) -> dict[str, Period]:
    df = _read(GOLDEN / "periods.csv")
    df = df[df["anchor"] == anchor.isoformat()]
    assert not df.empty, f"no golden periods for {anchor}; add it to PERIOD_ANCHORS"
    dates = ["start", "end", "prior_start", "prior_end"]
    return {
        row["key"]: Period(
            row["key"],
            row["title"],
            row["label"],
            row["prior_label"],
            *(date.fromisoformat(row[c]) for c in dates),
        )  # fmt: skip
        for _, row in df.iterrows()
    }


def numbers(name: str) -> pd.DataFrame:
    """A golden table of numbers (`metrics`, `metrics_by_category`, `metrics_by_payment_type`)
    with NULL as NaN or NaT, booleans as bool, `*_ts` as timestamps and the rest as floats."""
    df = _read(GOLDEN / f"{name}.csv")
    out = {}
    for column in df.columns:
        values = df[column]
        if column in _TEXT_COLUMNS:
            out[column] = values
        elif column in _BOOL_COLUMNS:
            out[column] = values.map({"true": True, "false": False})
        elif column.endswith("_ts"):
            out[column] = pd.to_datetime(values.replace("", None))
        else:
            out[column] = values.map(lambda v: math.nan if v == "" else float(v))
    return pd.DataFrame(out)
