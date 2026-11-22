"""Flatten nested Moka records into flat tables (dotted columns, child tables)."""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

# Moka labels timestamps "+07:00", but the clock time already is Bali local time.
# So the offset is dropped, never converted.
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?:Z|[+-]\d{2}:\d{2})$")


def naive_timestamp(value: Any) -> Any:
    """Return an ISO string without offset if `value` is an offset-labelled timestamp."""
    if isinstance(value, str):
        m = _TS.match(value)
        if m:
            return m.group(1)
    return value


def flat(rec: dict, prefix: str = "") -> dict:
    """Nested dicts become dotted columns. Lists of objects are dropped (child tables)."""
    out: dict = {}
    for k, v in rec.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat(v, key + "."))
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                continue
            out[key] = json.dumps(v) if v else ""
        else:
            out[key] = naive_timestamp(v)
    return out


class Table:
    """Rows of one flat table. Child tables carry the parent keys."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, rec: dict, **parent: Any) -> None:
        """Add a record. Parent keys always win; a clashing child field is kept under a
        `child_` prefix so no information is lost (e.g. a line's own `payment_id`)."""
        row = dict(parent)
        for k, v in flat(rec).items():
            row[f"child_{k}" if k in parent else k] = v
        self.rows.append(row)

    def to_frame(self) -> pd.DataFrame:
        """DataFrame with stable column order and Parquet-safe dtypes."""
        cols: list[str] = []
        for r in self.rows:
            for c in r:
                if c not in cols:
                    cols.append(c)
        df = pd.DataFrame(self.rows, columns=cols)
        for c in df.columns:
            df[c] = _tidy_column(df[c])
        return df


def _tidy_column(s: pd.Series) -> pd.Series:
    if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
        return s
    vals = s.dropna()
    vals = vals[vals != ""]
    if len(vals) and all(isinstance(v, str) and _is_naive_iso(v) for v in vals):
        return pd.to_datetime(s.where(s != "", None), format="ISO8601")
    kinds = {type(v) for v in s.dropna()}
    if kinds <= {bool}:
        return s
    if kinds <= {int, float} and kinds:
        return pd.to_numeric(s)
    return s.map(lambda v: v if v is None or (isinstance(v, float) and v != v) else str(v))


_NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")


def _is_naive_iso(v: str) -> bool:
    return bool(_NAIVE.match(v))
