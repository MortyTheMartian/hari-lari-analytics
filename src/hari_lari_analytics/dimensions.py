"""Derived dimensions: time truncations and item-variant labels."""

from __future__ import annotations

import pandas as pd


def _item_variant(item: pd.Series, variant: pd.Series) -> pd.Series:
    return item.where(variant.isna(), item + "-" + variant.fillna(""))


def add_dimensions(lines: pd.DataFrame) -> pd.DataFrame:
    df = lines.copy()
    ts = df["sale_ts"]
    df["sale_hour"] = ts.dt.floor("h")
    df["sale_date"] = ts.dt.normalize()
    df["sale_week"] = df["sale_date"] - pd.to_timedelta(ts.dt.day_of_week, unit="D")
    iso = ts.dt.isocalendar()
    df["sale_iso_week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    df["sale_month"] = ts.dt.to_period("M").dt.to_timestamp()
    df["sale_quarter"] = ts.dt.year.astype(str) + "-Q" + ts.dt.quarter.astype(str)
    df["sale_year"] = ts.dt.year
    df["hour_of_day"] = ts.dt.hour
    df["item_variant"] = _item_variant(df["item"], df["variant"])
    df["adjusted_item_variant"] = _item_variant(df["adjusted_item"], df["variant"])
    return df
