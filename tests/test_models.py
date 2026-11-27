from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.load import (
    InputError,
    load_aliases,
    load_calendar_events,
    load_category_groups,
)
from hari_lari_analytics.reporting.db import (
    RAW_SOURCES,
    Seeds,
    build_order,
    connect,
    load_sources,
    model_references,
    open_run,
)
from hari_lari_analytics.reporting.model_specs import model_sql

from .api_fixture import api_tables
from .conftest import FIXTURES
from .golden import assert_lines, cases

SEEDS = Seeds(
    item_aliases=load_aliases(FIXTURES / "item_aliases.csv"),
    category_groups=load_category_groups(FIXTURES / "category_group.csv"),
    calendar_events=load_calendar_events(FIXTURES / "calendar_events.csv"),
)
LAST_SALE = date(2026, 10, 13)


def _value(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, (datetime, date, np.datetime64)):
        return pd.Timestamp(v)
    if isinstance(v, np.generic):
        return v.item()
    return v


def _comparable(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = pd.DataFrame({c: [_value(v) for v in df[c]] for c in columns})
    return out.sort_values(columns, key=lambda s: s.astype(str)).reset_index(drop=True)


def golden_case(case: str):
    """The models of a case of `tests/golden/cases.py`, after checking that their sales lines
    equal the golden lines of the case."""
    con = cases.connect(case)
    assert_lines(con.execute("select * from fct_sales_lines").df(), case)
    return con


@pytest.mark.parametrize("case", ["fixture", "anchor_2026-10-06"])
def test_sales_lines_equal_the_golden_lines(case):
    golden_case(case)


def test_default_anchor_is_the_day_of_the_last_payment():
    con = connect(api_tables(), SEEDS)
    assert con.execute("select anchor from run_context").fetchone()[0] == LAST_SALE


def _query(con, sql: str) -> list[tuple]:
    return con.execute(sql).fetchall()


def test_line_note_is_carried():
    con = connect(api_tables(), SEEDS)
    notes = _query(con, "select receipt_nr, note from fct_sales_lines where note is not null")
    assert notes == [("R002", "oat milk please")]


def test_orders_have_their_attributes():
    con = connect(api_tables(), SEEDS)
    rows = _query(
        con,
        """
        select receipt_nr, payment_type, net_sales, items, lines, has_food, has_drink,
               has_discount, has_modifier, has_refund, counts_as_order
        from fct_orders order by receipt_nr
        """,
    )
    assert rows == [
        ("R001", "BCA", 68000, 2, 2, False, True, False, True, False, True),
        ("R002", "Cash", 30000, 1, 1, False, True, True, False, False, True),
        # fully refunded: the refund subtracts, the payment's time and type are kept
        ("R003", "Gopay", 0, 0, 1, False, True, False, False, True, False),
        ("R004", "BCA", 100000, 3, 3, True, False, False, False, False, True),
        ("R005", "BCA", 15000, 1, 1, False, False, False, False, False, True),
    ]
    r003 = _query(con, "select sale_ts from fct_orders where receipt_nr = 'R003'")
    assert r003 == [(datetime(2026, 10, 6, 9, 0),)]


def test_lines_carry_the_order_attributes():
    con = connect(api_tables(), SEEDS)
    rows = _query(
        con,
        "select item, has_drink, has_food, order_items from fct_sales_lines "
        "where receipt_nr = 'R004' order by item",
    )
    assert rows == [
        ("Mystery Cake", False, True, 3),
        ("Nasi Campur", False, True, 3),
        ("Nasi Campur", False, True, 3),
    ]


def test_order_and_line_totals_agree():
    con = connect(api_tables(), SEEDS)
    lines = _query(con, "select sum(net_sales), sum(quantity) from fct_sales_lines")
    orders = _query(con, "select sum(net_sales), sum(items) from fct_orders")
    assert lines == orders == [(213000, 7)]


def test_calendar_has_every_day_and_the_trading_days():
    con = connect(api_tables(), SEEDS)
    days = _query(con, "select date, weekday, is_trading_day from dim_calendar order by date")
    assert days[0] == (date(2026, 10, 5), 1, True)
    assert days[-1][0] == LAST_SALE
    assert len(days) == 9
    # 6 Oct only has the fully refunded receipt
    assert [d for d, _, trading in days if trading] == [
        date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 13)
    ]  # fmt: skip


def test_calendar_events_join_onto_days_orders_and_lines():
    con = connect(api_tables(), SEEDS)
    days = _query(
        con,
        "select date, event_kind, event_name, is_holiday, is_closure, has_promotion "
        "from dim_calendar where date >= '2026-10-05' order by date",
    )
    assert days[0] == (date(2026, 10, 5), None, None, False, False, False)
    assert days[1] == (date(2026, 10, 6), "holiday", "Demo Day", True, False, False)
    assert days[2][1:3] == ("closure", "Closed")
    # two events on one day: both names, and the holiday outranks the promotion
    assert days[-1] == (LAST_SALE, "holiday", "Two for one + Harvest Day", True, False, True)

    orders = _query(
        con, "select receipt_nr, event_kind, has_promotion from fct_orders order by receipt_nr"
    )
    assert orders == [
        ("R001", None, False),
        ("R002", None, False),
        ("R003", "holiday", False),
        ("R004", "promotion", True),
        ("R005", "holiday", True),
    ]
    lines = _query(con, "select distinct event_name from fct_sales_lines where receipt_nr = 'R004'")
    assert lines == [("Two for one",)]


def test_without_calendar_events_the_columns_are_empty():
    seeds = Seeds(SEEDS.item_aliases, SEEDS.category_groups)
    con = connect(api_tables(), seeds)
    assert _query(con, "select count(event_kind), bool_or(is_holiday) from dim_calendar") == [
        (0, False)
    ]


def test_items_and_categories_dimensions():
    con = connect(api_tables(), SEEDS)
    items = dict(
        (row[0], row[1:])
        for row in _query(
            con,
            "select adjusted_item, adjusted_category, category_group_level_1, is_live, "
            "first_sale_date from dim_items",
        )
    )
    assert items["Focaccia Tomato"] == ("Snacks (Focaccia)", "unmapped", True, date(2026, 10, 5))
    assert items["Mystery Cake"] == ("Cakes", "unmapped", False, date(2026, 10, 12))
    assert items["Nasi Campur"][:3] == ("Mains", "food", True)

    categories = dict(
        (row[0], row[1:])
        for row in _query(
            con, "select adjusted_category, category_group_level_1, is_live from dim_categories"
        )
    )
    assert categories["Coffee"] == ("drink", True)
    assert categories["Cakes"] == ("unmapped", False)


def test_item_resolution_flags_unmatched_items():
    con = connect(api_tables(), SEEDS)
    rows = _query(
        con, "select item, resolved_by from int_item_resolution where is_unmatched order by 1"
    )
    assert rows == [("Mystery Cake", "sales fallback")]


def test_optional_tables_may_be_missing_or_empty():
    tables = api_tables()
    del tables["transaction_line_modifiers"]
    tables["transaction_refunds"] = pd.DataFrame()  # an empty Parquet file has no columns
    con = connect(tables, SEEDS)
    assert _query(con, "select count(*), count(modifier_id) from fct_sales_lines") == [(9, 0)]
    reason = _query(con, "select refund_reason from fct_sales_lines where is_refund")
    assert reason == [(None,)]


def test_required_table_and_columns():
    tables = api_tables()
    del tables["items"]
    with pytest.raises(InputError, match="no table items"):
        connect(tables, SEEDS)
    tables = api_tables()
    tables["transactions"] = tables["transactions"].drop(columns="payment_type_label")
    with pytest.raises(InputError, match="payment_type_label"):
        connect(tables, SEEDS)


def test_the_run_tables_come_from_sources_yml(tmp_path):
    assert RAW_SOURCES["item_variants"].required is False
    assert RAW_SOURCES["transactions"].required is True
    path = tmp_path / "sources.yml"
    path.write_text("stock:\n  columns: { id: bigint, level: money }\n")
    with pytest.raises(ValueError, match=r"sources.yml: stock: unknown column type\(s\) money"):
        load_sources(path)
    path.write_text("stock:\n  columns: { id: bigint }\n  requried: false\n")
    with pytest.raises(ValueError, match="sources.yml: stock: requried: Extra inputs"):
        load_sources(path)


def test_build_order_refuses_a_table_that_is_not_declared():
    with pytest.raises(ValueError, match="stg_x reads raw_stock, which is not a run table of"):
        build_order({"stg_x": "create or replace view stg_x as select * from raw_stock"})
    with pytest.raises(ValueError, match="stg_x reads seed_menu, which is not a seed"):
        build_order({"stg_x": "create or replace view stg_x as select * from seed_menu"})
    with pytest.raises(ValueError, match="fct_x reads int_gone, which is not a model"):
        build_order({"fct_x": "create or replace view fct_x as select * from int_gone"})


def test_stock_is_a_snapshot_of_the_tracked_live_variants_at_the_extraction():
    at = datetime(2026, 10, 14, 9, 30)
    con = connect(api_tables(), SEEDS, extracted_at=at)
    rows = _query(
        con,
        "select adjusted_item_variant, in_stock, alert_at, stock_status, counted_at "
        "from fct_stock order by variant_id",
    )
    assert rows == [
        ("Latte-Hot", 0, 5, "out", at),
        ("Latte-Cold", 3, 5, "low", at),
        ("Cappucino-Hot", 40, 5, "ok", at),
        ("Focaccia Tomato", -7, 0, "negative", at),  # no stock alert: alert at 0
    ]
    tables = api_tables()
    del tables["item_variants"]  # a run from before the table was extracted
    assert _query(connect(tables, SEEDS), "select count(*) from fct_stock") == [(0,)]


# --- item identity by id (plan 9.8) --------------------------------------------------------------


def _latte_labels(case: str):
    """The item-variant label of each Latte line of a golden case, by receipt, with the label
    as sold next to it."""
    return _query(
        golden_case(case),
        "select receipt_nr, adjusted_item_variant, item_variant from fct_sales_lines "
        "where adjusted_item = 'Latte' order by receipt_nr",
    )


def test_a_live_variant_gives_todays_label_and_item_variant_stays_as_sold():
    assert _latte_labels("fixture") == [
        ("R001", "Latte-Hot", "Latte-Hot"),
        ("R002", "Latte-Cold", "Latte-Cold"),
    ]
    assert _latte_labels("variant_hot_renamed") == [
        ("R001", "Latte-Large", "Latte-Hot"),
        ("R002", "Latte-Cold", "Latte-Cold"),
    ]


def test_a_live_variant_without_a_name_is_the_item_alone():
    assert _latte_labels("variant_hot_without_name") == [
        ("R001", "Latte", "Latte-Hot"),
        ("R002", "Latte-Cold", "Latte-Cold"),
    ]


def test_a_deleted_or_unknown_variant_falls_back_to_the_item():
    assert _latte_labels("variant_cold_deleted") == [
        ("R001", "Latte", "Latte-Hot"),
        ("R002", "Latte", "Latte-Cold"),
    ]
    assert _latte_labels("variant_cold_unknown")[1] == ("R002", "Latte", "Latte-Cold")


def test_a_variant_of_another_item_does_not_name_the_line():
    assert _latte_labels("variant_cold_of_another_item")[1] == ("R002", "Latte", "Latte-Cold")


def test_a_run_without_the_variants_table_keeps_the_label_of_the_sale():
    assert _latte_labels("no_variants") == [
        ("R001", "Latte-Hot", "Latte-Hot"),
        ("R002", "Latte-Cold", "Latte-Cold"),
    ]


# --- item resolution by id and alias, discount codes ---------------------------------------------


def _line(case: str, receipt: str, item: str, columns: str) -> tuple:
    con = golden_case(case)
    sql = f"select {columns} from fct_sales_lines where receipt_nr = ? and item = ?"
    (row,) = con.execute(sql, [receipt, item]).fetchall()
    return row


def test_a_renamed_item_follows_its_id_without_an_alias():
    row = _line("item_renamed_without_alias", "R001", "Tomato Focaccia",
                "adjusted_item, adjusted_category")  # fmt: skip
    assert row == ("Focaccia Tomato", "Snacks (Focaccia)")


def test_an_alias_wins_over_the_item_id():
    # item 3 is "Focaccia Tomato" today, but the alias says this sales name is something else
    row = _line("item_alias_wins", "R001", "Tomato Focaccia", "adjusted_item, adjusted_category")
    assert row == ("Focaccia Mushroom", "Snacks")


def test_a_deleted_item_falls_back_to_its_alias_then_to_its_own_category():
    columns = "adjusted_item, adjusted_category"
    # the alias's capitalization is corrected to the live item's
    row = _line("item_deleted_with_alias", "R001", "Tomato Focaccia", columns)
    assert row == ("Focaccia Tomato Special", "Snacks (Focaccia)")
    row = _line("item_deleted_without_alias", "R001", "Tomato Focaccia", columns)
    assert row == ("Tomato Focaccia", "Snacks (Focaccia)")


def test_several_discount_codes_on_one_line_are_joined():
    (code,) = _line("two_discount_codes", "R002", "Latte", "discount_code")
    assert code == "CLEANUP20 + FOUNDER25"


def test_build_order_puts_references_first_and_refuses_upward_references():
    models = {
        "fct_a": "create or replace view fct_a as select * from int_b join dim_c using (x)",
        "int_b": "create or replace view int_b as select * from stg_d",
        "dim_c": "create or replace view dim_c as select * from int_b",
        "stg_d": "create or replace view stg_d as select * from raw_transactions",
    }
    order = build_order(models)
    assert order.index("stg_d") < order.index("int_b") < order.index("dim_c") < order.index("fct_a")
    with pytest.raises(ValueError, match="higher layer"):
        build_order({"int_x": "create or replace view int_x as select * from fct_a", **models})


def test_build_order_reads_the_sql_with_duckdb_not_the_comments_and_strings():
    models = {
        # names in a comment, in a string and in a block comment are not references
        "int_b": (
            "create or replace view int_b as\n-- reads fct_a, but only in this comment\n"
            "select 'dim_c' as note, * from stg_d /* fct_a */"
        ),
        "fct_a": "create or replace view fct_a as select * from int_b",
        "stg_d": "create or replace view stg_d as select * from raw_transactions",
    }
    assert model_references(models) == {
        "int_b": {"stg_d"},
        "fct_a": {"int_b"},
        "stg_d": {"raw_transactions"},
    }
    order = build_order(models)  # a reference upwards (int_b reads fct_a) would raise
    assert order.index("stg_d") < order.index("int_b") < order.index("fct_a")


def test_build_order_reads_joins_subqueries_ctes_and_unions():
    models = {
        "fct_a": (
            "create or replace view fct_a as with c as (select * from int_b)\n"
            "select title_case(x), week_start(d), (select max(y) from dim_c) as m "
            "from c join Stg_D as d using (k) where k in (select k from raw_transactions) "
            "union all select 1, 2, 3 from main.seed_item_aliases, generate_series(1, 3)"
        ),
        "int_b": "create or replace view int_b as select 1 as x",
        "dim_c": "create or replace view dim_c as select 1 as y",
        "stg_d": "create or replace view stg_d as select 1 as k",
    }
    # `c` is a CTE, not a table; names are case-folded; functions need not exist; USING needs
    # no tables to exist, since DuckDB only parses
    assert model_references(models)["fct_a"] == {
        "int_b", "dim_c", "stg_d", "raw_transactions", "seed_item_aliases",
    }  # fmt: skip
    assert build_order(models).index("fct_a") == 3


def test_build_order_says_which_model_cannot_be_read():
    with pytest.raises(ValueError, match=r"model int_x: DuckDB cannot read its SQL.*syntax error"):
        build_order({"int_x": "create or replace view int_x as select from from"})
    with pytest.raises(ValueError, match="model int_x must define the view int_x"):
        build_order({"int_x": "select 1"})
    with pytest.raises(ValueError, match=r"model int_x: DuckDB cannot read its SQL.*SELECT"):
        build_order({"int_x": "create or replace view int_x as insert into t values (1)"})


def test_the_real_models_depend_on_what_they_read_not_on_their_comments():
    models = model_sql()
    # fct_orders names fct_sales_lines in a comment only
    assert "fct_sales_lines" in models["fct_orders"]
    references = model_references(models)
    assert references["fct_orders"] == {"dim_calendar", "int_order_attributes"}
    assert references["fct_sales_lines"] == {
        "dim_calendar", "int_order_attributes", "int_sales_lines",
    }  # fmt: skip
    assert references["stg_transactions"] == {"raw_transactions"}
    order = build_order(models)  # also proves that no model reads a higher layer
    assert sorted(order) == sorted(models)
    for name, refs in references.items():
        assert all(order.index(r) < order.index(name) for r in refs if r in models)


REAL_RUN = Path("data/extract/2026-11-22")


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run (kept out of git)")
def test_the_real_run_has_the_lines_the_pandas_pipeline_had():
    db = open_run(REAL_RUN, Path("config"), date(2026, 11, 22))
    assert _query(db.con, "select count(*) from fct_sales_lines") == [(2707,)]


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run (kept out of git)")
def test_every_day_without_sales_is_a_closure_in_the_real_calendar():
    db = open_run(REAL_RUN, Path("config"), date(2026, 11, 22))
    rows = _query(
        db.con,
        "select count(*) filter (where not is_trading_day), "
        "count(*) filter (where not is_trading_day and not is_closure), "
        "count(*) filter (where is_trading_day and is_closure) from dim_calendar",
    )
    assert rows == [(32, 0, 0)]
