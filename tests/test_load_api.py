from pathlib import Path

import pandas as pd
import pytest

from hari_lari_analytics.clean import MODIFIER_COLUMNS, clean_sales
from hari_lari_analytics.load import (
    InputError,
    find_inputs,
    load_aliases,
    load_library,
    load_sales,
)
from hari_lari_analytics.load_api import api_sales_lines, find_run, load_api_sales, load_run

from .api_fixture import api_tables
from .conftest import FIXTURES

ALIASES = pd.DataFrame(
    {
        "sales_item": ["Tomato Focaccia", "Custom Amount"],
        "adjusted_item": ["Focaccia Tomato", ""],
        "adjusted_category": ["", "Custom Amount"],
    }
)
NO_ALIASES = ALIASES.iloc[0:0]

COMPARED = [
    "receipt_nr", "item", "variant", "category", "quantity", "gross_sales", "discounts",
    "refunds", "net_sales", "discount_code", "payment_type", "refund_reason", "is_refund",
    "discounted", "counts_as_order", "adjusted_item", "adjusted_category",
]  # fmt: skip


def _comparable(lines: pd.DataFrame) -> pd.DataFrame:
    df = lines[COMPARED].copy()
    df["variant"] = df["variant"].fillna("")
    df = df.astype(object).fillna("∅")
    return df.sort_values(COMPARED[:10], kind="stable").reset_index(drop=True)


def _api(aliases=ALIASES, **replace) -> pd.DataFrame:
    tables = {**api_tables(), **replace}
    return api_sales_lines(tables, aliases).lines


def _line(lines: pd.DataFrame, receipt: str, item: str) -> pd.Series:
    return lines[(lines["receipt_nr"] == receipt) & (lines["item"] == item)].iloc[0]


def test_matches_the_csv_pipeline_on_the_same_sales():
    library_path, sales_path = find_inputs(FIXTURES)
    csv = clean_sales(
        load_sales(sales_path),
        load_library(library_path),
        load_aliases(FIXTURES / "item_aliases.csv"),
    )
    api = api_sales_lines(api_tables(), ALIASES)
    pd.testing.assert_frame_equal(_comparable(api.lines), _comparable(csv.lines))


def test_has_the_ids():
    lines = _api()
    assert {"payment_id", "item_id", "variant_id", "category_id"} <= set(lines)
    latte = _line(lines, "R002", "Latte")
    assert (latte["payment_id"], latte["item_id"], latte["variant_id"]) == ("p2", 1, 11)


def test_refund_has_the_opposite_sign():
    lines = _api()
    refund = lines[lines["is_refund"]].iloc[0]
    assert refund["event_type"] == "Refund"
    assert refund["quantity"] == -1
    assert (refund["gross_sales"], refund["discounts"]) == (0, 0)
    assert refund["refunds"] == 40000
    assert refund["net_sales"] == -40000
    assert refund["refund_reason"] == "Accidental Charge"
    # The refunded item and category are the ones of the sale, with no text parsing
    assert (refund["item"], refund["adjusted_category"]) == ("Cappucino", "Coffee")


def test_gross_minus_discounts_minus_refunds_is_net_on_every_line():
    lines = _api()
    assert (
        (lines["gross_sales"] - lines["discounts"] - lines["refunds"]).eq(lines["net_sales"]).all()
    )


def test_fully_refunded_receipt_does_not_count_as_order():
    per_receipt = _api().groupby("receipt_nr")["counts_as_order"].all()
    assert not per_receipt["R003"]
    assert per_receipt.drop("R003").all()


def test_renamed_item_follows_its_id_without_an_alias():
    lines = _api(NO_ALIASES)
    focaccia = _line(lines, "R001", "Tomato Focaccia")
    assert focaccia["adjusted_item"] == "Focaccia Tomato"
    assert focaccia["adjusted_category"] == "Snacks (Focaccia)"


def test_alias_wins_over_the_item_id():
    # item 3 is "Focaccia Tomato" today, but the alias says this sales name is something else
    aliases = pd.DataFrame(
        {
            "sales_item": ["Tomato Focaccia"],
            "adjusted_item": ["Focaccia Mushroom"],
            "adjusted_category": ["Snacks"],
        }
    )
    focaccia = _line(_api(aliases), "R001", "Tomato Focaccia")
    assert (focaccia["adjusted_item"], focaccia["adjusted_category"]) == (
        "Focaccia Mushroom",
        "Snacks",
    )


def test_deleted_item_falls_back_to_alias_then_to_its_own_category():
    tables = api_tables()
    tables["items"].loc[tables["items"]["name"] == "Focaccia Tomato", "is_deleted"] = True
    live = pd.DataFrame(
        [(7, "Focaccia Tomato Special", "Snacks (Focaccia)", False)],
        columns=tables["items"].columns,
    )
    tables["items"] = pd.concat([tables["items"], live], ignore_index=True)

    aliased = pd.DataFrame(
        {
            "sales_item": ["Tomato Focaccia"],
            "adjusted_item": ["focaccia tomato special"],  # capitalization is corrected
            "adjusted_category": [""],
        }
    )
    row = _line(api_sales_lines(tables, aliased).lines, "R001", "Tomato Focaccia")
    assert row["adjusted_item"] == "Focaccia Tomato Special"

    result = api_sales_lines(tables, NO_ALIASES)
    row = _line(result.lines, "R001", "Tomato Focaccia")
    assert row["adjusted_item"] == "Tomato Focaccia"
    assert row["adjusted_category"] == "Snacks (Focaccia)"
    # a deleted item that is still in the catalog needs no note
    assert not any("Tomato Focaccia" in note for note in result.notes)


def test_item_missing_from_the_catalog_is_noted():
    result = api_sales_lines(api_tables(), ALIASES)
    assert any("Mystery Cake" in note for note in result.notes)
    mystery = _line(result.lines, "R004", "Mystery Cake")
    assert mystery["adjusted_category"] == "Cakes"


def test_several_discount_codes_on_one_line_are_joined():
    tables = api_tables()
    extra = tables["transaction_line_discounts"].assign(discount_name="CLEANUP20")
    tables["transaction_line_discounts"] = pd.concat(
        [tables["transaction_line_discounts"], extra], ignore_index=True
    )
    lines = api_sales_lines(tables, ALIASES).lines
    assert _line(lines, "R002", "Latte")["discount_code"] == "CLEANUP20 + FOUNDER25"


def test_partial_refund_amount_mismatch_is_noted():
    tables = api_tables()
    tables["transactions"].loc[tables["transactions"]["id"] == "p3r", "refund_amount"] = 25000
    result = api_sales_lines(tables, ALIASES)
    assert any("refund" in note and "differ" in note for note in result.notes)


def test_broken_amounts_stop_the_run():
    tables = api_tables()
    tables["transaction_lines"].loc[0, "net_sales"] = 1.0
    with pytest.raises(InputError, match="Net Sales"):
        api_sales_lines(tables, ALIASES)


def test_fractional_rupiah_are_an_error():
    tables = api_tables()
    tables["transaction_lines"].loc[0, "gross_sales"] = 100.5
    with pytest.raises(InputError, match="fractional"):
        api_sales_lines(tables, ALIASES)


def _write_run(root: Path, name: str, tables: dict[str, pd.DataFrame]) -> Path:
    run = root / name
    (run / "tables").mkdir(parents=True)
    for table, df in tables.items():
        df.to_parquet(run / "tables" / f"{table}.parquet", index=False)
    (run / "manifest.json").write_text('{"last_sale": "2026-10-13T11:00:00"}')
    return run


def test_load_run_takes_the_latest_dated_run(tmp_path):
    _write_run(tmp_path, "2026-10-12", api_tables())
    latest = _write_run(tmp_path, "2026-10-14", api_tables())
    assert find_run(tmp_path) == str(latest)
    assert find_run(latest) == str(latest)

    run = load_run(tmp_path)
    assert run.name == "2026-10-14"
    assert run.manifest["last_sale"] == "2026-10-13T11:00:00"
    _, result = load_api_sales(tmp_path, ALIASES)
    assert len(result.lines) == 9


def test_load_run_without_a_run_is_an_input_error(tmp_path):
    with pytest.raises(InputError, match="No extraction run"):
        find_run(tmp_path)


def test_missing_table_is_an_input_error(tmp_path):
    tables = api_tables()
    del tables["items"]
    run = _write_run(tmp_path, "2026-10-14", tables)
    with pytest.raises(InputError, match="missing table"):
        load_run(run)


REAL_INPUT = Path("data/input")
# The run the parity was established on: a later run files past sales under the catalog of its
# day (items moved from Non Coffee to Extras after 22 Nov 2026), unknown to the October export.
REAL_EXTRACT = Path("data/extract/2026-11-22")


@pytest.mark.skipif(
    not (REAL_INPUT.is_dir() and REAL_EXTRACT.is_dir()),
    reason="needs the real back-office export and an API extraction run (kept out of git)",
)
def test_parity_with_the_real_back_office_export():
    """1 Jan to 31 Oct 2026: 948 receipts and 1,700 lines, identical line by line."""
    # The CSV pipeline resolves items by name and needs the full alias list (frozen copy);
    # the API pipeline follows item ids and only needs the short list in config/.
    full_aliases = load_aliases(FIXTURES / "item_aliases_full_2026-11.csv")
    library_path, sales_path = find_inputs(REAL_INPUT)
    csv = clean_sales(load_sales(sales_path), load_library(library_path), full_aliases).lines
    _, result = load_api_sales(REAL_EXTRACT, load_aliases(Path("config/item_aliases.csv")))
    api = result.lines
    api = api[api["sale_ts"] < "2026-11-01"]

    assert (csv["receipt_nr"].nunique(), len(csv)) == (948, 1700)
    assert (api["receipt_nr"].nunique(), len(api)) == (948, 1700)
    pd.testing.assert_frame_equal(_comparable(api), _comparable(csv))


def test_modifiers_are_on_the_line_without_changing_its_amounts():
    lines = _api()
    focaccia = _line(lines, "R001", "Tomato Focaccia")
    # two modifiers: ordered by option id, so ids, names and options line up
    assert focaccia["modifier_option_id"] == "9012 + 9021"
    assert focaccia["modifier_id"] == "901 + 902"
    assert focaccia["modifier_name"] == "Breakfast Add-ons + Egg Preference"
    assert focaccia["modifier_option_name"] == "Chicken + Poached"
    assert focaccia["modifier_price"] == 20000
    assert focaccia["gross_sales"] == 28000  # one row per line: amounts are untouched
    assert len(lines) == 9


def test_lines_without_modifiers_have_null_modifier_columns():
    lines = _api()
    plain = lines[lines["item"] != "Tomato Focaccia"]
    for column in MODIFIER_COLUMNS:
        assert plain[column].isna().all(), column


def test_without_a_modifier_table_the_columns_are_null():
    tables = api_tables()
    del tables["transaction_line_modifiers"]
    lines = api_sales_lines(tables, ALIASES).lines
    assert set(MODIFIER_COLUMNS) <= set(lines)
    assert lines[MODIFIER_COLUMNS].isna().all().all()


def test_csv_pipeline_has_the_same_null_columns():
    library_path, sales_path = find_inputs(FIXTURES)
    csv = clean_sales(
        load_sales(sales_path),
        load_library(library_path),
        load_aliases(FIXTURES / "item_aliases.csv"),
    ).lines
    assert csv[MODIFIER_COLUMNS].isna().all().all()
