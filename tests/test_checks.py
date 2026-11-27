from datetime import date
from pathlib import Path

import duckdb
import pydantic
import pytest

from hari_lari_analytics.reporting.checks import (
    DataCheckError,
    check,
    load_checks,
    run_checks,
)
from hari_lari_analytics.reporting.db import connect, open_run

from .api_fixture import api_tables
from .test_models import REAL_RUN, SEEDS


@pytest.fixture
def con():
    con = duckdb.connect()
    con.execute(
        """
        create table orders as select * from (values
            ('R1', 10, 'cash'), ('R2', 20, 'card'), ('R2', 5, 'card'), ('R3', null, 'gold')
        ) as t(receipt, amount, payment);
        create table lines as select * from (values ('R1', 10), ('R2', 25), ('R9', 1))
            as t(receipt, amount);
        """
    )
    return con


def _run(con, text: str):
    return run_checks(con, load_checks(text))


def _failures(con, text: str) -> list[tuple[str, int, list]]:
    return [(r.check.name, r.failures, r.sample) for r in _run(con, text).results if not r.passed]


def test_unique_single_and_composite(con):
    assert _failures(con, "models: {orders: {unique: [receipt, [receipt, amount]]}}") == [
        ("orders: receipt is unique", 1, [("R2", 2)])
    ]


def test_not_null_shows_the_sample_columns(con):
    text = "models: {orders: {sample_columns: [receipt], not_null: [amount, payment]}}"
    assert _failures(con, text) == [("orders: amount is not null", 1, [(None, "R3")])]


def test_accepted_values(con):
    text = "models: {orders: {accepted_values: {payment: [cash, card]}}}"
    assert _failures(con, text) == [("orders: payment in ('cash', 'card')", 1, [("gold",)])]


def test_relationships(con):
    text = "models: {lines: {relationships: {receipt: orders.receipt}}}"
    assert _failures(con, text) == [("lines: every receipt is in orders.receipt", 1, [("R9",)])]


def test_expressions_treat_null_as_failing(con):
    text = "models: {orders: {sample_columns: [receipt], expressions: [amount > 0]}}"
    assert _failures(con, text) == [("orders: amount > 0", 1, [("R3",)])]


def test_totals_show_both_sides(con):
    text = "totals: [sum(orders.amount) = sum(lines.amount), count(orders.receipt) = 4]"
    assert _failures(con, text) == [
        ("totals: sum(orders.amount) = sum(lines.amount)", 1, [(35, 36)])
    ]


def test_warnings_do_not_stop_the_build_and_errors_do(con):
    warn = """
    models:
      orders:
        not_null:
          - {column: amount, severity: warn, message: an order without amount}
    """
    report = check(con, load_checks(warn))
    assert [r.describe() for r in report.warnings] == [
        "an order without amount: 1 failing row(s), e.g. None"
    ]
    with pytest.raises(DataCheckError, match=r"1 data check\(s\) failed:\n- orders: amount"):
        check(con, load_checks("models: {orders: {not_null: [amount]}}"))


def test_unknown_keys_and_bad_sql_are_errors(con):
    with pytest.raises(pydantic.ValidationError, match="not_nul"):
        load_checks("models: {orders: {not_nul: [amount]}}")
    with pytest.raises(pydantic.ValidationError, match="severity"):
        load_checks("models: {orders: {not_null: [{column: amount, severity: fatal}]}}")
    with pytest.raises(ValueError, match="not valid SQL"):
        _run(con, "models: {orders: {not_null: [no_such_column]}}")


def _errors(tables) -> list[str]:
    return [r.check.message or r.check.name for r in run_checks(connect(tables, SEEDS)).errors]


def test_the_fixture_passes_with_the_expected_warnings():
    report = check(connect(api_tables(), SEEDS))
    assert not report.errors
    assert sorted(r.check.message for r in report.warnings) == [
        "days without sales and without a closure in config/calendar_events.csv",
        "sold categories without a group in config/category_group.csv",
    ]


def test_fractional_rupiah_fail():
    tables = api_tables()
    tables["transaction_lines"].loc[0, "gross_sales"] = 15000.5
    assert "the API data contains fractional Rupiah amounts" in _errors(tables)


def test_broken_amounts_fail():
    tables = api_tables()
    tables["transaction_lines"].loc[0, "net_sales"] = 1.0
    assert "int_signed_lines: gross_sales - discounts - refunds = net_sales" in _errors(tables)


def test_a_refund_whose_lines_differ_from_its_refund_amount_warns():
    tables = api_tables()
    payments = tables["transactions"]
    payments.loc[payments["id"] == "p3r", "refund_amount"] = 30000
    report = check(connect(tables, SEEDS))
    (warning,) = [r for r in report.warnings if "partial refund" in r.check.message]
    assert warning.sample == [("R003", 30000.0)]
    assert "refunds whose line amounts differ" in warning.describe()


def test_a_line_without_its_payment_fails():
    tables = api_tables()
    tables["transactions"] = tables["transactions"][tables["transactions"]["id"] != "p5"]
    assert "a line has no matching payment" in _errors(tables)


def test_a_payment_listed_twice_fails():
    tables = api_tables()
    tables["transactions"] = tables["transactions"].iloc[[0, *range(len(tables["transactions"]))]]
    errors = _errors(tables)
    assert "stg_transactions: payment_id is unique" in errors
    assert "fct_sales_lines: payment_id, line_uuid is unique" in errors


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run (kept out of git)")
def test_the_real_run_passes_and_only_warns_about_the_unmapped_categories():
    db = open_run(REAL_RUN, Path("config"), date(2026, 11, 22))
    assert [sorted(r.sample) for r in db.checks.warnings] == [[("Custom Amount",), ("Other",)]]
