"""Derived dimensions: time truncations and item-variant labels."""

from __future__ import annotations

import pandas as pd

from hari_lari_analytics.project import project


def _item_variant(item: pd.Series, variant: pd.Series) -> pd.Series:
    return item.where(variant.isna(), item + "-" + variant.fillna(""))


def add_dimensions(lines: pd.DataFrame) -> pd.DataFrame:
    df = lines.copy()
    ts = df["sale_ts"]
    df["sale_hour"] = ts.dt.floor("h")
    df["sale_date"] = ts.dt.normalize()
    days_into_week = (ts.dt.day_of_week + 1 - project().first_isoweekday) % 7
    df["sale_week"] = df["sale_date"] - pd.to_timedelta(days_into_week, unit="D")
    iso = ts.dt.isocalendar()
    df["sale_iso_week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    df["sale_month"] = ts.dt.to_period("M").dt.to_timestamp()
    df["sale_quarter"] = ts.dt.year.astype(str) + "-Q" + ts.dt.quarter.astype(str)
    df["sale_year"] = ts.dt.year
    df["hour_of_day"] = ts.dt.hour
    df["item_variant"] = _item_variant(df["item"], df["variant"])
    df["adjusted_item_variant"] = _item_variant(df["adjusted_item"], df["variant"])
    return df


UNMAPPED = "unmapped"


def add_category_groups(
    lines: pd.DataFrame, groups: pd.DataFrame
) -> tuple[pd.DataFrame, list[str]]:
    """Add `category_group_level_1` and `_level_2` by adjusted category, plus a note if needed.

    Categories without a row in `config/category_group.csv` get level 1 `unmapped` (a warning,
    not a failure). An empty level 2 stays missing.
    """
    df = lines.copy()
    level_1 = groups.set_index("category")["category_group_level_1"]
    level_2 = groups.set_index("category")["category_group_level_2"].replace("", pd.NA)
    df["category_group_level_1"] = df["adjusted_category"].map(level_1).fillna(UNMAPPED)
    df["category_group_level_2"] = df["adjusted_category"].map(level_2).astype(object)
    is_unmapped = df["category_group_level_1"] == UNMAPPED
    unmapped = sorted(df.loc[is_unmapped, "adjusted_category"].unique())
    notes = []
    if unmapped:
        notes.append(
            f"{len(unmapped)} categor(y/ies) have no group in config/category_group.csv and are "
            f"'{UNMAPPED}': " + ", ".join(unmapped)
        )
    return df, notes
