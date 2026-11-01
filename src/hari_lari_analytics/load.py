"""Find and read the two Moka POS exports, and check that they look as expected."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

LIBRARY_PATTERN = re.compile(r"^\d{4}_\d{2}_\d{2}_item_library\.csv$")
SALES_PATTERN = re.compile(
    r"^\d{4}_\d{2}_\d{2}_sales_report_item_details_\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{4}\.csv$"
)

SALES_COLUMNS = [
    "Receipt Number",
    "Date",
    "Time",
    "Category",
    "Items",
    "Variant",
    "Quantity",
    "Discount Applied",
    "Gross Sales",
    "Discounts",
    "Refunds",
    "Net Sales",
    "Payment Method",
    "Event Type",
    "Reason of Refund",
]
MONEY_COLUMNS = ["Gross Sales", "Discounts", "Refunds", "Net Sales"]

LIBRARY_ITEM = "Items Name (Do Not Edit)"
LIBRARY_COLUMNS = [LIBRARY_ITEM, "Category", "Variant name"]

ALIAS_COLUMNS = ["sales_item", "adjusted_item", "adjusted_category"]


class InputError(Exception):
    """The input files are missing, ambiguous or do not have the expected structure."""


def find_inputs(input_dir: Path) -> tuple[Path, Path]:
    """Return (item_library_path, sales_report_path); exactly one of each must exist."""
    if not input_dir.is_dir():
        raise InputError(f"Input folder not found: {input_dir}")
    names = sorted(p.name for p in input_dir.iterdir() if p.is_file())
    library = [n for n in names if LIBRARY_PATTERN.match(n)]
    sales = [n for n in names if SALES_PATTERN.match(n)]
    problems = []
    for label, found, example in [
        ("item library", library, "2026_11_01_item_library.csv"),
        (
            "sales report",
            sales,
            "2026_11_01_sales_report_item_details_01-01-2026_31-10-2026.csv",
        ),
    ]:
        if not found:
            problems.append(f"no {label} file found (expected a name like {example})")
        elif len(found) > 1:
            problems.append(f"more than one {label} file found: {', '.join(found)}")
    if problems:
        raise InputError(f"In {input_dir}: " + "; ".join(problems))
    return input_dir / library[0], input_dir / sales[0]


def _read_csv(path: Path, required: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise InputError(f"{path.name} is missing columns: {', '.join(missing)}")
    return df


def _to_int(series: pd.Series, column: str, path: Path) -> pd.Series:
    values = pd.to_numeric(series.replace("", "0"), errors="coerce")
    if values.isna().any():
        bad = series[values.isna()].unique()[:5]
        raise InputError(f"{path.name}: non-numeric values in '{column}': {list(bad)}")
    if (values.round() != values).any():
        raise InputError(f"{path.name}: '{column}' contains fractional values")
    return values.round().astype("int64")


def load_sales(path: Path) -> pd.DataFrame:
    """Read the itemized sales report with amounts as whole Rupiah and Quantity as int."""
    df = _read_csv(path, SALES_COLUMNS)
    for column in MONEY_COLUMNS + ["Quantity"]:
        df[column] = _to_int(df[column], column, path)

    mismatch = df["Gross Sales"] - df["Discounts"] - df["Refunds"] != df["Net Sales"]
    if mismatch.any():
        receipts = df.loc[mismatch, "Receipt Number"].unique()[:5]
        raise InputError(
            f"{path.name}: Gross - Discounts - Refunds != Net Sales on {mismatch.sum()} rows "
            f"(e.g. receipts {list(receipts)})"
        )
    unknown_events = set(df["Event Type"]) - {"Payment", "Refund"}
    if unknown_events:
        raise InputError(f"{path.name}: unexpected Event Type values {sorted(unknown_events)}")
    return df


def load_library(path: Path) -> pd.DataFrame:
    """Read the item library; one row per item and variant."""
    df = _read_csv(path, LIBRARY_COLUMNS)
    df[LIBRARY_ITEM] = df[LIBRARY_ITEM].str.strip()
    df["Category"] = df["Category"].str.strip()
    categories = df.groupby(LIBRARY_ITEM)["Category"].nunique()
    conflicting = categories[categories > 1].index.tolist()
    if conflicting:
        raise InputError(f"{path.name}: items listed under several categories: {conflicting}")
    return df


def load_aliases(path: Path) -> pd.DataFrame:
    """Read the item alias file (sales item name -> current item name / category)."""
    if not path.exists():
        return pd.DataFrame(columns=ALIAS_COLUMNS)
    df = _read_csv(path, ALIAS_COLUMNS)
    for column in ALIAS_COLUMNS:
        df[column] = df[column].str.strip()
    duplicated = df.loc[df["sales_item"].duplicated(), "sales_item"].tolist()
    if duplicated:
        raise InputError(f"{path.name}: sales_item listed more than once: {duplicated}")
    return df
