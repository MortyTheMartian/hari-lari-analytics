"""Read the Parquet tables of one Moka API extraction run and build the cleaned sales lines.

The result has the same columns as `clean.clean_sales` (so metrics and reports do not care
where the data came from), plus the Moka ids `payment_id`, `item_id`, `variant_id` and
`category_id`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from hari_lari_analytics.load import InputError

if TYPE_CHECKING:
    from hari_lari_analytics.clean import CleanResult

# The pandas mapping (`api_sales_lines` and the helpers below it) imports `clean` when it runs,
# so that reading a run does not load the pandas pipeline.

REQUIRED_TABLES = ["transactions", "transaction_lines", "items"]
OPTIONAL_TABLES = [
    "transaction_line_discounts",
    "transaction_line_modifiers",
    "transaction_refunds",
    "item_variants",  # the labels of the lines follow their variants' ids (plan 9.8)
]


@dataclass
class ApiRun:
    """The tables of one extraction run, with the manifest if there is one."""

    location: str
    tables: dict[str, pd.DataFrame]
    manifest: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.location.rstrip("/").rsplit("/", 1)[-1]


def _is_gcs(location: str) -> bool:
    return location.startswith("gs://")


def find_run(location: str | Path) -> str:
    """Return the folder of one run.

    `location` is either a run folder (it holds `tables/`) or a folder of dated runs, in which
    case the most recent run wins. `gs://bucket/prefix` works the same way.
    """
    location = str(location)
    if _is_gcs(location):
        return _find_gcs_run(location)
    root = Path(location)
    if (root / "tables").is_dir():
        return str(root)
    runs = sorted(p for p in root.glob("*") if (p / "tables").is_dir()) if root.is_dir() else []
    if not runs:
        raise InputError(f"No extraction run (a folder with tables/) found in {root}")
    return str(runs[-1])


def _gcs_client():
    from google.cloud import storage  # needs the `gcp` extra

    return storage.Client()


def _split_gcs(location: str) -> tuple[str, str]:
    bucket, _, prefix = location[len("gs://") :].partition("/")
    return bucket, prefix.strip("/")


def _find_gcs_run(location: str, client=None) -> str:
    client = client or _gcs_client()
    bucket, prefix = _split_gcs(location)
    base = f"{prefix}/" if prefix else ""
    if any(True for _ in client.list_blobs(bucket, prefix=f"{base}tables/", max_results=1)):
        return f"gs://{bucket}/{prefix}"  # `location` is itself a run folder
    blobs = client.list_blobs(bucket, prefix=base)
    runs = {
        b.name[len(base) :].split("/", 1)[0]
        for b in blobs
        if b.name[len(base) :].count("/") >= 2 and b.name[len(base) :].split("/")[1] == "tables"
    }
    if not runs:
        raise InputError(f"No extraction run (a folder with tables/) found in {location}")
    return f"gs://{bucket}/{base}{sorted(runs)[-1]}"


def load_run(location: str | Path, tables: Iterable[str] | None = None) -> ApiRun:
    """Read the Parquet tables (and manifest) of the run in `location`: `tables` (default: the
    tables of today's report), each if the run has it, and always the required ones."""
    names = list(dict.fromkeys([*REQUIRED_TABLES, *(tables or OPTIONAL_TABLES)]))
    run = find_run(location)
    if _is_gcs(run):
        with tempfile.TemporaryDirectory() as tmp:
            local = _download_gcs(run, Path(tmp))
            loaded = _read_local(local, names)
    else:
        loaded = _read_local(Path(run), names)
    return ApiRun(run, loaded[0], loaded[1])


def _download_gcs(location: str, target: Path, client=None) -> Path:
    client = client or _gcs_client()
    bucket, prefix = _split_gcs(location)
    for blob in client.list_blobs(bucket, prefix=prefix + "/"):
        rel = blob.name[len(prefix) + 1 :]
        if rel.startswith("tables/") or rel == "manifest.json":
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dest))
    return target


def _read_local(run_dir: Path, names: list[str]) -> tuple[dict[str, pd.DataFrame], dict]:
    tables: dict[str, pd.DataFrame] = {}
    for name in names:
        path = run_dir / "tables" / f"{name}.parquet"
        if path.exists():
            tables[name] = pd.read_parquet(path)
    missing = [n for n in REQUIRED_TABLES if n not in tables]
    if missing:
        raise InputError(f"{run_dir}: missing table(s) {', '.join(missing)}")
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    return tables, manifest


def api_sales_lines(tables: dict[str, pd.DataFrame], aliases: pd.DataFrame) -> CleanResult:
    """Turn the API tables into cleaned sales lines (see the module docstring)."""
    from hari_lari_analytics.clean import CleanResult, fully_refunded_receipts

    notes: list[str] = []
    df = _lines_frame(tables, notes)
    df["counts_as_order"] = ~df["receipt_nr"].isin(fully_refunded_receipts(df))
    _check_identity(df)
    _resolve_items(df, tables["items"], aliases, notes)
    return CleanResult(
        lines=df.sort_values("sale_ts", kind="stable").reset_index(drop=True),
        notes=notes,
        item_variants=tables.get("item_variants"),
    )


def _lines_frame(tables: dict[str, pd.DataFrame], notes: list[str]) -> pd.DataFrame:
    from hari_lari_analytics.clean import UNCATEGORIZED

    tx = tables["transactions"]
    lines = tables["transaction_lines"]

    payments = tx[["id", "payment_no", "created_at", "parent_payment_id", "payment_type_label"]]
    payments = payments.rename(columns={"id": "payment_id", "created_at": "sale_ts"})
    payments["is_refund"] = payments["payment_id"].ne(payments["parent_payment_id"])
    df = lines.merge(payments, on=["payment_id", "payment_no"], how="left", validate="m:1")
    if df["sale_ts"].isna().any():
        raise InputError(f"{int(df['sale_ts'].isna().sum())} lines have no matching payment")

    refund = df["is_refund"]
    # A refund payment repeats the refunded lines with positive amounts. Turn them around:
    # negative quantity, nothing gross, the amount in Refunds (Gross - Discounts - Refunds = Net).
    gross = df["gross_sales"].where(~refund, 0)
    discounts = df["discount_amount"].where(~refund, 0)
    refunds = df["net_sales"].where(refund, 0)
    result = pd.DataFrame(
        {
            "receipt_nr": df["payment_no"].str.strip(),
            "sale_ts": df["sale_ts"].dt.floor("s"),  # like the back-office export: whole seconds
            "event_type": refund.map({True: "Refund", False: "Payment"}),
            "is_refund": refund,
            "refund_reason": _refund_reasons(tables, df),
            "category": _blank_to_na(df["category_name"]).fillna(UNCATEGORIZED),
            "item": df["item_name"].str.strip(),
            "variant": _blank_to_na(df["item_variant_name"]).str.title(),
            "quantity": df["quantity"].where(~refund, -df["quantity"]).astype("int64"),
            "gross_sales": _idr(gross),
            "discounts": _idr(discounts),
            "refunds": _idr(refunds),
            "net_sales": _idr(df["net_sales"].where(~refund, -df["net_sales"])),
            "discount_code": _discount_codes(tables, df),
            "payment_type": _blank_to_na(df["payment_type_label"]),
            "payment_id": df["payment_id"],
            "item_id": df["item_id"],
            "variant_id": df["item_variant_id"],
            "category_id": df["category_id"],
        }
    )
    result = pd.concat([result, _modifiers(tables, df)], axis=1)
    result["discounted"] = result["discount_code"].notna() | result["discounts"].gt(0)
    _check_refund_totals(tx, result, notes)
    return result


def _blank_to_na(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace("", pd.NA).astype(object)


def _idr(series: pd.Series) -> pd.Series:
    """Whole Rupiah as int64; fractional amounts are an input error."""
    if (series.round() != series).any():
        raise InputError("The API data contains fractional Rupiah amounts")
    return series.round().astype("int64")


def _refund_reasons(tables: dict[str, pd.DataFrame], df: pd.DataFrame) -> pd.Series:
    refunds = tables.get("transaction_refunds")
    if refunds is None or refunds.empty:
        return pd.Series(pd.NA, index=df.index, dtype=object)
    reasons = refunds.drop_duplicates("payment_id").set_index("payment_id")["refunded_reason"]
    reason = df["payment_id"].map(reasons)
    return _blank_to_na(reason).where(df["is_refund"], pd.NA)


def _discount_codes(tables: dict[str, pd.DataFrame], df: pd.DataFrame) -> pd.Series:
    """Discount name of the line. Refund lines carry none; several codes are joined by ' + '."""
    discounts = tables.get("transaction_line_discounts")
    if discounts is None or discounts.empty:
        return pd.Series(pd.NA, index=df.index, dtype=object)
    names = (
        discounts.groupby(["payment_id", "line_uuid"])["discount_name"]
        .agg(lambda s: " + ".join(sorted(set(s.dropna().str.strip()))))
        .rename("discount_code")
    )
    keyed = df[["payment_id", "uuid"]].join(names, on=["payment_id", "uuid"])
    return _blank_to_na(keyed["discount_code"]).where(~df["is_refund"], pd.NA)


def _modifiers(tables: dict[str, pd.DataFrame], df: pd.DataFrame) -> pd.DataFrame:
    """Modifiers (add-ons and choices such as "Chicken" or "Poached") of each line.

    A line keeps one row, so its amounts still add up: Moka already includes the modifier
    prices in the line's gross sales. The columns are NULL for lines without modifiers. A line
    with several modifiers lists them joined by ' + ', in the order of the option id, so the
    ids, names and option names line up; `modifier_price` is the sum of their unit prices.
    """
    from hari_lari_analytics.clean import MODIFIER_COLUMNS

    empty = pd.DataFrame(pd.NA, index=df.index, columns=MODIFIER_COLUMNS, dtype=object)
    empty["modifier_price"] = empty["modifier_price"].astype("Int64")
    mods = tables.get("transaction_line_modifiers")
    if mods is None or mods.empty:
        return empty

    def join(series: pd.Series) -> str:
        return " + ".join(str(v).strip() for v in series)

    mods = mods.sort_values("modifier_option_id", kind="stable")
    grouped = mods.groupby(["payment_id", "line_uuid"])
    per_line = pd.DataFrame(
        {
            "modifier_id": grouped["modifier_id"].agg(join),
            "modifier_option_id": grouped["modifier_option_id"].agg(join),
            "modifier_name": grouped["modifier_name"].agg(join),
            "modifier_option_name": grouped["modifier_option_name"].agg(join),
            "modifier_price": grouped["price"].sum(),
        }
    )
    keyed = df[["payment_id", "uuid"]].join(per_line, on=["payment_id", "uuid"])
    out = keyed[MODIFIER_COLUMNS].astype(object)
    out = out.where(out.notna(), pd.NA)
    out["modifier_price"] = pd.to_numeric(out["modifier_price"]).astype("Int64")
    return out


def _check_identity(df: pd.DataFrame) -> None:
    broken = df["gross_sales"] - df["discounts"] - df["refunds"] != df["net_sales"]
    if broken.any():
        receipts = df.loc[broken, "receipt_nr"].unique()[:5]
        raise InputError(
            f"API data: Gross - Discounts - Refunds != Net Sales on {int(broken.sum())} lines "
            f"(e.g. receipts {list(receipts)})"
        )


def _check_refund_totals(tx: pd.DataFrame, lines: pd.DataFrame, notes: list[str]) -> None:
    """Warn when the refunded line amounts differ from the refund amount Moka reports."""
    refunded = lines.loc[lines["is_refund"]].groupby("payment_id")["refunds"].sum()
    reported = tx.set_index("id")["refund_amount"]
    gap = {
        pid: (amount, reported.get(pid, 0))
        for pid, amount in refunded.items()
        if amount != reported.get(pid, 0)
    }
    if gap:
        notes.append(
            f"{len(gap)} refund(s) have line amounts that differ from the refund amount "
            "reported by Moka (partial refund?); the line amounts are used."
        )


def _resolve_items(
    df: pd.DataFrame, items: pd.DataFrame, aliases: pd.DataFrame, notes: list[str]
) -> None:
    """Add `adjusted_item` and `adjusted_category` (in place), looked up by item id.

    Per sold item (item id + name as sold):

    1. `config/item_aliases.csv` wins when it lists the sales name: `adjusted_item` replaces
       the name (empty: keep the sales name), `adjusted_category` sets the category.
    2. Otherwise a live item gives today's name and category, matched by its id, so renamed
       items follow their new name without an alias.
    3. A deleted item without alias keeps its sales name, unless the name matches a live
       item (ignoring capitalization), and the category it last had in Moka. An item that is
       not in the catalog at all uses its latest sales-report category and is noted.

    Today's spelling and category of the adjusted name always come from the live items.
    """
    from hari_lari_analytics.clean import UNCATEGORIZED

    current = items.set_index("id")
    live = items.loc[~items["is_deleted"]].drop_duplicates("name")
    live_category = live.set_index("name")["category.name"]
    live_name = {name.casefold(): name for name in live["name"]}
    alias_rows = aliases.set_index("sales_item").to_dict("index") if len(aliases) else {}

    latest = (
        df.loc[~df["is_refund"] & df["category"].ne(UNCATEGORIZED)]
        .sort_values("sale_ts")
        .groupby("item")["category"]
        .last()
        .to_dict()
    )

    adjusted: dict[tuple[int, str], tuple[str, str]] = {}
    unmatched: set[str] = set()
    for item_id, name in df[["item_id", "item"]].drop_duplicates().itertuples(index=False):
        alias = alias_rows.get(name)
        today = current.loc[item_id] if item_id in current.index else None
        is_live = today is not None and not today["is_deleted"]
        if alias is not None:
            item = alias.get("adjusted_item") or name
        elif is_live:
            item = today["name"]
        else:
            item = name
        canonical = live_name.get(item.casefold())
        item = canonical or item

        if alias is not None and alias.get("adjusted_category"):
            category = alias["adjusted_category"]
        elif canonical:
            category = live_category[canonical]
        elif today is not None and today["category.name"] != UNCATEGORIZED:
            category = today["category.name"]
        else:
            category = latest.get(name, UNCATEGORIZED)
        if not canonical and today is None and not (alias and alias.get("adjusted_category")):
            unmatched.add(name)
        adjusted[(item_id, name)] = (item, category)

    keys = list(zip(df["item_id"], df["item"], strict=True))
    df["adjusted_item"] = [adjusted[k][0] for k in keys]
    df["adjusted_category"] = [adjusted[k][1] for k in keys]

    if unmatched:
        notes.append(
            f"{len(unmatched)} item name(s) are neither in the Moka item catalog nor in "
            f"config/item_aliases.csv and use their latest sales-report category: "
            + ", ".join(sorted(unmatched))
        )
    uncategorized = sorted({n for (_, n), (_, c) in adjusted.items() if c == UNCATEGORIZED})
    if uncategorized:
        notes.append(
            f"{len(uncategorized)} item name(s) end up as '{UNCATEGORIZED}': "
            + ", ".join(uncategorized)
        )


def load_api_sales(location: str | Path, aliases: pd.DataFrame) -> tuple[ApiRun, CleanResult]:
    """Find the latest run in `location` and build the cleaned lines from it."""
    run = load_run(location)
    return run, api_sales_lines(run.tables, aliases)
