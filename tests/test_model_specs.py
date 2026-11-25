"""The model metadata of `models.yml` (plan 7A.3): validation, the primary keys in the checks,
the time column, trading days and the sample size in the compiler, and a fictional model
`fct_expenses` without orders that loads, compiles and runs a windowed query.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hari_lari_analytics.reporting.checks import CheckFile, compile_checks, load_checks
from hari_lari_analytics.reporting.compiler import Query, QueryError, run_query
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.model_specs import (
    ModelError,
    ModelSpec,
    default_models,
    load_models,
    model_sql,
)
from hari_lari_analytics.reporting.registry import default_registry, load_registry
from hari_lari_analytics.reporting.transforms import TransformContext, apply

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

REG = default_registry()


@pytest.fixture(scope="module")
def con():
    return connect(api_tables(), SEEDS, LAST_SALE)


def columns(con, model: str) -> set[str]:
    return {row[0] for row in con.execute(f"describe {model}").fetchall()}


# --- models.yml ---------------------------------------------------------------------------------


def test_every_model_has_an_entry_with_existing_columns(con):
    models = default_models()
    assert set(models) == set(model_sql())
    for model in models.values():
        assert model.description, model.id
        assert set(model.primary_key) <= columns(con, model.id), model.id
        if model.time:
            assert model.time in columns(con, model.id), model.id
    exposed = {m for m, spec in models.items() if spec.exposed}
    assert exposed == {"dim_items", "dim_categories", "dim_calendar", "fct_sales_lines",
                       "fct_orders", "fct_stock"}  # fmt: skip
    assert models["fct_orders"].sample_size == "orders" and models["fct_orders"].calendar


@pytest.mark.parametrize(
    "text, message",
    [
        ("fct_x: {time: day}", "fct_x: description: Field required"),
        ("fct_x: {description: d, grain: line}", "fct_x: grain: Extra inputs are not permitted"),
        ("fct_x: {description: d, calendar: true}", "fct_x: calendar needs the time column"),
        ("stg_x: {description: d, exposed: true}", "stg_x: only dim_ and fct_ models"),
        ("[a, b]", "expected a mapping of model names"),
    ],
)
def test_invalid_entries(text, message):
    with pytest.raises(ModelError, match="^models.yml: " + message):
        load_models(text)


def test_the_entries_match_the_sql_models():
    with pytest.raises(ModelError, match="no entry for the model.s. fct_b"):
        load_models("fct_a: {description: d}", names=["fct_a", "fct_b"])
    with pytest.raises(ModelError, match="fct_b is not a model in models/"):
        load_models("fct_a: {description: d}\nfct_b: {description: d}", names=["fct_a"])


def test_the_sample_size_is_a_count_metric_on_the_model():
    models = dict(default_models())
    models["fct_orders"] = models["fct_orders"].model_copy(update={"sample_size": "net_sales"})
    with pytest.raises(Exception, match="fct_orders: sample_size net_sales is not a count"):
        load_registry(models=models)


# --- the primary keys in the checks -------------------------------------------------------------


def test_the_primary_keys_are_checked_for_uniqueness():
    names = [c.name for c in compile_checks(load_checks(), default_models())]
    assert "fct_sales_lines: payment_id, line_uuid is unique" in names
    assert "dim_calendar: date is unique" in names
    assert "stg_items: item_id is unique" in names  # a warning of checks.yml, not a key
    without_models = [c.name for c in compile_checks(load_checks())]
    assert "dim_calendar: date is unique" not in without_models


def test_a_primary_key_is_not_declared_twice():
    checks = CheckFile.model_validate({"models": {"fct_orders": {"unique": ["receipt_nr"]}}})
    with pytest.raises(ValueError, match="fct_orders: the primary key is unique by models.yml"):
        compile_checks(checks, default_models())


# --- the compiler reads the models --------------------------------------------------------------


def test_only_exposed_models_with_a_time_column_can_be_queried(con):
    with pytest.raises(QueryError, match="int_sales_lines is not exposed"):
        run_query(con, Query(model="int_sales_lines", metrics=["net_sales"], window="full"), REG)
    with pytest.raises(QueryError, match="dim_items has no time column"):
        run_query(con, Query(model="dim_items", metrics=["net_sales"], window="full"), REG)


def test_the_sample_size_columns_follow_the_model(con):
    r = run_query(con, Query(model="fct_orders", metrics=["aov"], window="full"), REG)
    assert "n_orders" in r.df and r.meta.metrics["aov"].min_n == {"orders": 30}


# --- a model without orders ---------------------------------------------------------------------

EXPENSE_MODELS = {
    "fct_expenses": ModelSpec(
        id="fct_expenses",
        description="One expense",
        primary_key=["expense_id"],
        time="expense_date",
        exposed=True,
    ),  # fmt: skip
}
EXPENSE_METRICS = """
amount:
  label: Expenses
  description: Expenses paid
  format: idr
  up_is_good: false
  additivity: full
  sql: { fct_expenses: sum(amount) }
expense_share:
  label: "% of expenses"
  description: Share of all expenses
  format: pct
  up_is_good: null
  additivity: full
  relative: amount / nullif(total.amount, 0)
"""
EXPENSE_DIMENSIONS = """
bucket: { label: Period, grain: day, models: [fct_expenses], domain: calendar }
expense_category: { label: Expense category, grain: line, models: [fct_expenses] }
"""


@pytest.fixture(scope="module")
def expenses():
    con = connect(api_tables(), SEEDS, LAST_SALE)
    con.execute("""
        create table fct_expenses as select * from (values
            (1, date '2026-10-05', 'Rent', 1000000.0),
            (2, date '2026-10-12', 'Milk', 200000.0),
            (3, date '2026-10-13', 'Milk', 50000.0)
        ) as v(expense_id, expense_date, expense_category, amount)
    """)  # fmt: skip
    registry = load_registry(EXPENSE_METRICS, EXPENSE_DIMENSIONS, "[]", EXPENSE_MODELS)
    return con, registry


def test_a_model_without_orders_loads(expenses):
    _, registry = expenses
    assert registry.min_n("amount", "fct_expenses") == {}  # no sample_size: no rule


def test_a_windowed_query_without_orders(expenses):
    con, registry = expenses
    r = run_query(con, Query(model="fct_expenses", metrics=["amount"], window="WTD",
                             compare="prior"), registry)  # fmt: skip
    row = r.df.iloc[0]
    assert (row.amount, row.prior_amount) == (250_000, 1_000_000)
    assert not [c for c in r.df.columns if c.startswith("n_") or c.startswith("prior_n_")]
    assert r.meta.trading_days is None  # the calendar does not apply
    assert "expense_date between" in r.meta.sql


def test_groups_series_and_the_sample_size_rule_without_orders(expenses):
    con, registry = expenses
    r = run_query(con, Query(model="fct_expenses", dimensions=["expense_category"],
                             metrics=["amount", "expense_share"], window="full", totals=True),
                  registry)  # fmt: skip
    assert r.rows["expense_category"].tolist() == ["Milk", "Rent"]  # sorted by value
    assert r.rows["expense_share"].tolist() == pytest.approx([0.2, 0.8])
    assert pd.isna(r.total.trading_days)
    kept = apply("suppress_small_n", {}, r, TransformContext(registry))
    assert kept.df["amount"].tolist() == r.df["amount"].tolist()  # nothing is small
    series = run_query(con, Query(model="fct_expenses", dimensions=["bucket"], metrics=["amount"],
                                  window="full", grain="week"), registry)  # fmt: skip
    assert series.df["amount"].tolist() == [1_000_000, 250_000]
