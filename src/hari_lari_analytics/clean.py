"""Turn the raw Moka export into tidy sales lines with adjusted items and categories."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from hari_lari_analytics.load import LIBRARY_ITEM

UNCATEGORIZED = "Uncategorized"
REFUND_ITEM = re.compile(r"Refund for: \S+ - (.+)$")


@dataclass
class CleanResult:
    lines: pd.DataFrame
    notes: list[str] = field(default_factory=list)


@dataclass
class _Resolved:
    adjusted_item: str
    adjusted_category: str
    source: str  # "alias", "library" or "sales fallback"


def _blank_to_na(series: pd.Series) -> pd.Series:
    return series.str.strip().replace("", pd.NA)


def clean_sales(sales: pd.DataFrame, library: pd.DataFrame, aliases: pd.DataFrame) -> CleanResult:
    is_refund = sales["Event Type"].eq("Refund")
    df = pd.DataFrame(
        {
            "receipt_nr": sales["Receipt Number"].str.strip(),
            "sale_ts": pd.to_datetime(
                sales["Date"].str.strip() + " " + sales["Time"].str.strip(),
                format="%d-%m-%Y %H:%M:%S",
            ),
            "event_type": sales["Event Type"],
            "is_refund": is_refund,
            "refund_reason": _blank_to_na(sales["Reason of Refund"]),
            "category": _blank_to_na(sales["Category"]).fillna(UNCATEGORIZED),
            "item": sales["Items"].str.strip(),
            "variant": _blank_to_na(sales["Variant"]).str.title(),
            "quantity": sales["Quantity"],
            "gross_sales": sales["Gross Sales"],
            "discounts": sales["Discounts"],
            "refunds": sales["Refunds"],
            "net_sales": sales["Net Sales"],
            "discount_code": _blank_to_na(sales["Discount Applied"]),
            "payment_type": _blank_to_na(sales["Payment Method"]),
        }
    )
    df["discounted"] = df["discount_code"].notna() | df["discounts"].gt(0)
    notes: list[str] = []

    _attribute_refunds(df, notes)
    df["counts_as_order"] = ~df["receipt_nr"].isin(_fully_refunded_receipts(df))

    resolved = _resolve_items(df, library, aliases, notes)
    df["adjusted_item"] = df["item"].map(lambda i: resolved[i].adjusted_item)
    df["adjusted_category"] = df["item"].map(lambda i: resolved[i].adjusted_category)
    return CleanResult(lines=df, notes=notes)


def _attribute_refunds(df: pd.DataFrame, notes: list[str]) -> None:
    """Point refund lines ("Full Refund for: <receipt> - <item>") back to the refunded item."""
    refund_idx = df.index[df["is_refund"]]
    if refund_idx.empty:
        return
    extracted = df.loc[refund_idx, "item"].str.extract(REFUND_ITEM)[0]
    parsed = extracted.notna()
    df.loc[refund_idx[parsed.to_numpy()], "item"] = extracted[parsed]
    if (~parsed).any():
        notes.append(
            f"{int((~parsed).sum())} refund line(s) did not name the refunded item; "
            "they are reported under their own item name."
        )

    originals = (
        df.loc[~df["is_refund"], ["receipt_nr", "item", "category", "variant"]]
        .drop_duplicates(["receipt_nr", "item"])
        .set_index(["receipt_nr", "item"])
    )
    keys = pd.MultiIndex.from_frame(df.loc[refund_idx, ["receipt_nr", "item"]])
    matched = originals.reindex(keys)
    found = matched["category"].notna().to_numpy()
    for column in ["category", "variant"]:
        df.loc[refund_idx[found], column] = matched[column].to_numpy()[found]


def _fully_refunded_receipts(df: pd.DataFrame) -> pd.Index:
    per_receipt = df.groupby("receipt_nr").agg(
        has_refund=("is_refund", "any"), net_sales=("net_sales", "sum")
    )
    return per_receipt.index[per_receipt["has_refund"] & per_receipt["net_sales"].le(0)]


def _latest_sales_category(df: pd.DataFrame) -> dict[str, str]:
    """Most recent category per sales item, preferring a real category over Uncategorized."""
    payments = df.loc[~df["is_refund"]].sort_values("sale_ts")
    categorized = payments[payments["category"] != UNCATEGORIZED]
    latest = payments.groupby("item")["category"].last().to_dict()
    latest.update(categorized.groupby("item")["category"].last().to_dict())
    return latest


def _resolve_items(
    df: pd.DataFrame, library: pd.DataFrame, aliases: pd.DataFrame, notes: list[str]
) -> dict[str, _Resolved]:
    """Map every sales item name to today's item name and category.

    Order of precedence for the category: alias file override, then the item library,
    then the item's latest category in the sales report itself.
    """
    library_category = library.drop_duplicates(LIBRARY_ITEM).set_index(LIBRARY_ITEM)["Category"]
    library_name = {name.casefold(): name for name in library_category.index}
    alias_rows = aliases.set_index("sales_item").to_dict("index")
    latest_category = _latest_sales_category(df)

    resolved: dict[str, _Resolved] = {}
    for item in df["item"].unique():
        alias = alias_rows.get(item, {})
        adjusted_item = alias.get("adjusted_item") or item
        canonical = library_name.get(adjusted_item.casefold())
        if canonical:
            adjusted_item = canonical

        if alias.get("adjusted_category"):
            resolved[item] = _Resolved(adjusted_item, alias["adjusted_category"], "alias")
        elif canonical:
            resolved[item] = _Resolved(adjusted_item, library_category[canonical], "library")
        else:
            fallback = latest_category.get(item, UNCATEGORIZED)
            resolved[item] = _Resolved(adjusted_item, fallback, "sales fallback")

    fallbacks = sorted(i for i, r in resolved.items() if r.source == "sales fallback")
    if fallbacks:
        notes.append(
            f"{len(fallbacks)} item name(s) are neither in the item library nor in "
            f"config/item_aliases.csv and use their latest sales-report category: "
            + ", ".join(fallbacks)
        )
    uncategorized = sorted(i for i, r in resolved.items() if r.adjusted_category == UNCATEGORIZED)
    if uncategorized:
        notes.append(
            f"{len(uncategorized)} item name(s) end up as '{UNCATEGORIZED}': "
            + ", ".join(uncategorized)
        )
    return resolved
