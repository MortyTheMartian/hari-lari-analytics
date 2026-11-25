import json
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.load import load_aliases
from hari_lari_analytics.load_api import load_api_sales
from hari_lari_analytics.metrics import compute_metrics, contributive_aov
from hari_lari_analytics.reporting.db import connect, open_run
from hari_lari_analytics.reporting.registry import (
    Aggregate,
    RegistryError,
    default_registry,
    load_registry,
    parse_bins,
    render_expression,
)

from .api_fixture import api_tables
from .conftest import FIXTURES
from .semantic import evaluate
from .test_models import LAST_SALE, REAL_RUN, SEEDS, _pandas_lines

MODELS = ["fct_sales_lines", "fct_orders"]
REG = default_registry()


@pytest.fixture(scope="module")
def con():
    return connect(api_tables(), SEEDS, LAST_SALE)


def test_every_metric_of_metrics_md_and_the_plan_is_defined():
    metrics_md = ["gross_sales", "discounts", "refunds", "net_sales", "orders", "aov",
                  "items_sold", "penetration", "category_aov", "contributive_aov",
                  "first_sale_ts", "last_sale_ts"]  # fmt: skip
    plan = ["share", "discount_rate", "items_per_order", "avg_item_price",
            "net_sales_per_trading_day", "food_orders", "drink_orders", "food_and_drink_orders",
            "food_attach_rate", "drink_attach_rate"]  # fmt: skip
    assert set(metrics_md + plan) <= set(REG.metrics)
    kinds = {m.kind for m in REG.metrics.values()}
    assert kinds == {"sql", "filtered", "derived", "relative"}


def test_every_dimension_of_metrics_md_and_the_order_attributes_is_defined():
    expected = ["sale_hour", "sale_date", "sale_week", "sale_iso_week", "sale_month",
                "sale_quarter", "sale_year", "hour_of_day", "category", "adjusted_category",
                "item", "adjusted_item", "item_variant", "adjusted_item_variant", "discounted",
                "discount_code", "payment_type", "category_group_level_1",
                "category_group_level_2", "weekday", "has_food", "has_drink", "has_consignment",
                "has_discount", "has_modifier", "basket_size", "event_kind",
                "is_holiday"]  # fmt: skip
    assert set(expected) <= set(REG.dimensions)


def test_every_metric_has_its_attributes():
    for metric in REG.metrics.values():
        assert metric.label and metric.description, metric.id
        assert metric.format and metric.additivity, metric.id
        for model in metric.models:  # the sales models have orders as their sample size
            has_orders = REG.model(model).sample_size is not None
            assert bool(REG.min_n(metric.id, model)) == has_orders, (metric.id, model)


@pytest.mark.parametrize("model", MODELS)
def test_every_metric_runs_on_every_model_it_is_on(con, model):
    metrics = REG.metrics_on(model)
    stock = set(REG.metrics_on("fct_stock"))
    assert set(metrics) == set(REG.metrics) - stock  # every sales metric is on both models
    by_payment = evaluate(con, REG, model, metrics, by="payment_type", trading_days=3)
    assert len(by_payment) == 4  # BCA, Cash, Gopay and the total


@pytest.mark.parametrize("model", [*MODELS, "dim_calendar"])
def test_every_dimension_exists_on_its_models(con, model):
    for dim in REG.dimensions.values():
        if model in dim.models and dim.id != "bucket":  # the compiler computes the bucket
            con.execute(f"select {dim.column_on(model)} from {model} limit 0")


def test_every_calendar_dimension_is_on_dim_calendar():
    day = [d.id for d in REG.dimensions.values() if d.grain == "day" and d.id != "bucket"]
    assert day and all("dim_calendar" in REG.dimension(d).models for d in day)


def test_the_fixture_totals_by_hand(con):
    expected = {
        "gross_sales": 263000, "discounts": 10000, "refunds": 40000, "net_sales": 213000,
        "orders": 4,  # R003 is fully refunded
        "items_sold": 7, "aov": 53250, "items_per_order": 1.75, "avg_item_price": 213000 / 7,
        "discount_rate": 10000 / 263000, "net_sales_per_trading_day": 71000,
        "food_orders": 1, "drink_orders": 2, "food_and_drink_orders": 0,
        "food_attach_rate": 0.0, "drink_attach_rate": 0.0,
        "share": 1.0, "penetration": 1.0, "contributive_aov": 53250, "category_aov": 53250,
    }  # fmt: skip
    times = ["first_sale_ts", "last_sale_ts"]
    for model in MODELS:
        row = evaluate(con, REG, model, list(expected) + times, trading_days=3).iloc[0]
        assert {k: row[k] for k in expected} == pytest.approx(expected), model
        assert row[times].tolist() == [datetime(2026, 10, 5, 7), datetime(2026, 10, 13, 11)]


def test_the_categories_by_hand(con):
    rows = evaluate(
        con, REG, "fct_sales_lines",
        ["net_sales", "orders", "share", "penetration", "category_aov", "contributive_aov"],
        by="adjusted_category",
    ).set_index("adjusted_category")  # fmt: skip
    coffee = rows.loc["Coffee"]  # R001, R002 and the refunded R003
    assert coffee[["net_sales", "orders", "penetration", "category_aov", "contributive_aov"]]\
        .tolist() == [70000, 2, 0.5, 35000, 17500]  # fmt: skip
    assert coffee["share"] == pytest.approx(70000 / 213000)
    members = rows[~rows["is_total"]]
    assert members["contributive_aov"].sum() == pytest.approx(53250)
    assert members["share"].sum() == pytest.approx(1)


def test_filtered_metrics_work_on_lines_and_orders(con):
    metrics = ["food_orders", "drink_orders", "food_and_drink_orders"]
    by_day = [
        evaluate(con, REG, model, metrics, by="sale_date").sort_values("sale_date")[metrics]
        for model in MODELS
    ]
    pd.testing.assert_frame_equal(*(df.reset_index(drop=True) for df in by_day))


def _parity(con, lines: pd.DataFrame) -> None:
    """The registry metrics equal the pandas metrics of today's report."""
    columns = ["gross_sales", "discounts", "refunds", "net_sales", "orders", "items_sold",
               "aov", "first_sale_ts", "last_sale_ts"]  # fmt: skip
    expected = compute_metrics(lines).iloc[0]
    for model in MODELS:
        row = evaluate(con, REG, model, columns).iloc[0]
        assert row[columns[:-3]].tolist() == expected[columns[:-3]].tolist(), model
        assert row["aov"] == pytest.approx(expected["aov"], rel=1e-12)
        assert row[columns[-2:]].tolist() == expected[columns[-2:]].tolist(), model

    cats = contributive_aov(lines).set_index("adjusted_category")
    actual = evaluate(
        con, REG, "fct_sales_lines",
        ["net_sales", "items_sold", "orders", "penetration", "category_aov", "contributive_aov"],
        by="adjusted_category",
    )  # fmt: skip
    actual = actual[~actual["is_total"]].set_index("adjusted_category").loc[cats.index]
    for column in ["net_sales", "items_sold", "orders", "penetration", "contributive_aov"]:
        np.testing.assert_allclose(actual[column], cats[column], rtol=1e-12, err_msg=column)
    np.testing.assert_allclose(
        actual["category_aov"].astype(float), cats["category_aov"].astype(float), rtol=1e-12
    )

    by_payment = compute_metrics(lines, ["payment_type"]).set_index("payment_type")
    actual = evaluate(con, REG, "fct_orders", ["net_sales", "orders"], by="payment_type")
    actual = actual[~actual["is_total"]].set_index("payment_type").loc[by_payment.index]
    assert actual["orders"].tolist() == by_payment["orders"].tolist()
    assert actual["net_sales"].tolist() == by_payment["net_sales"].tolist()


def test_parity_with_the_pandas_metrics_on_the_fixture(con):
    lines = _pandas_lines(
        api_tables(), FIXTURES / "item_aliases.csv", FIXTURES / "category_group.csv", LAST_SALE
    )
    _parity(con, lines)


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run (kept out of git)")
def test_parity_with_the_pandas_metrics_on_the_real_run():
    from hari_lari_analytics.dimensions import prepare_lines

    anchor = date(2026, 11, 22)
    _, result = load_api_sales(REAL_RUN, load_aliases(Path("config/item_aliases.csv")))
    lines, _ = prepare_lines(result, Path("config/category_group.csv"), anchor)
    _parity(open_run(REAL_RUN, Path("config"), anchor).con, lines)


def test_expressions_render_references_totals_and_trading_days():
    assert REG.expression("penetration", ref=lambda m: f"c.{m}", total=lambda m: f"t.{m}") == (
        "c.orders / nullif(t.orders, 0)"
    )
    assert REG.expression("net_sales_per_trading_day", trading_days="$days") == (
        "net_sales / nullif($days, 0)"
    )


def test_a_plan_lists_the_inputs_first():
    plan = REG.plan(["food_attach_rate", "contributive_aov"], "fct_orders")
    assert set(plan.aggregates) == {"net_sales", "orders", "drink_orders", "food_and_drink_orders"}
    assert plan.computed == ["contributive_aov", "food_attach_rate"]


def test_metric_specs_for_the_visuals():
    assert REG.metric("discounts").up_is_good is False
    assert REG.metric("share").up_is_good is None  # neutral
    assert REG.metric("food_orders").format == "count"  # inherited from orders
    assert REG.metric("food_attach_rate").min_n == {"drink_orders": 30}
    assert REG.metric("aov").min_n is None  # the default of the model
    assert REG.min_n("aov", "fct_orders") == {"orders": 30}  # models.yml: sample_size orders
    assert REG.min_n("food_attach_rate", "fct_sales_lines") == {"drink_orders": 30}


def test_dimension_details():
    assert REG.dimension("basket_size").column_on("fct_orders") == "items"
    assert REG.dimension("basket_size").column_on("fct_sales_lines") == "order_items"
    assert [b.label for b in REG.dimension("basket_size").bin_list] == ["1", "2", "3", "4", "5+"]
    assert REG.dimension("weekday").labels[1] == "Mon"
    assert REG.dimension("has_food").labels[True] == "With food"
    with pytest.raises(RegistryError, match="not on fct_orders"):
        REG.dimension("adjusted_category").column_on("fct_orders")


def test_aggregates_parse_and_restrict():
    agg = Aggregate.parse("count(distinct receipt_nr) filter (where counts_as_order)")
    assert (agg.func, agg.distinct, agg.expr, agg.where) == (
        "count",
        True,
        "receipt_nr",
        "counts_as_order",
    )
    assert agg.restrict("has_food = true").sql == (
        "count(distinct receipt_nr) filter (where (counts_as_order) and (has_food = true))"
    )
    assert Aggregate.parse("sum(coalesce(x, 0))").sql == "sum(coalesce(x, 0))"


def test_bins():
    bins = parse_bins([1, "2-3", "4+"])
    assert [(b.low, b.high) for b in bins] == [(1, 1), (2, 3), (4, None)]
    for bad in ([1, "1-2"], ["3+", 4], ["3-2"], ["x"]):
        with pytest.raises(ValueError):
            parse_bins(bad)


# --- validation: each rule fails with the file and the entry -------------------------------

DIMS = """
has_food: {label: Food, grain: order, models: [fct_orders]}
payment_type: {label: Payment, grain: order, models: [fct_orders]}
"""
BASE = """
net_sales: {label: Net, description: d, format: idr, up_is_good: true, additivity: full,
            sql: {fct_orders: sum(net_sales), fct_sales_lines: sum(net_sales)}}
orders: {label: Orders, description: d, format: count, up_is_good: true,
         additivity: order_grain, sql: {fct_orders: count(*), fct_sales_lines: count(*)}}
"""


def _load(metrics: str = "", dims: str = DIMS, kpi: str = "[]"):
    return load_registry(BASE + metrics, dims, kpi)


@pytest.mark.parametrize(
    "metrics, message",
    [
        (
            "x: {label: X, description: d, format: idr, up_is_good: true, additivity: full,"
            " sql: {fct_orders: sum(a)}, fromat: idr}",
            "x: fromat: Extra inputs",
        ),
        (
            "x: {label: X, description: d, format: idr, up_is_good: true, additivity: full,"
            " sql: {fct_orders: sum(a)}, derived: net_sales}",
            "x: needs exactly one of",
        ),
        (
            "x: {label: X, description: d, format: idr, additivity: full, derived: net_sales}",
            "x: missing up_is_good",
        ),
        (
            "x: {label: X, description: d, format: idr, up_is_good: true, additivity: full,"
            " sql: {fct_orders: sum net_sales}}",
            "x: 'sum net_sales' is not an aggregate",
        ),
        (
            "x: {label: X, description: d, format: idr, up_is_good: true, additivity: full,"
            " sql: {fct_nothing: sum(a)}}",
            "x: unknown model",
        ),
        (
            "x: {label: X, description: d, format: idr, up_is_good: true, additivity: none,"
            " derived: net_sales / revenue}",
            "x: unknown metric(s) revenue",
        ),
        (
            "x: {label: X, description: d, format: pct, up_is_good: true, additivity: none,"
            " derived: orders / total.orders}",
            "x: total.<metric> is only allowed in relative",
        ),
        (
            "x: {label: X, description: d, format: pct, up_is_good: true, additivity: none,"
            " relative: orders / net_sales}",
            "x: a relative metric must use total",
        ),
        (
            "x: {label: X, description: d, format: pct, up_is_good: true, additivity: none,"
            " derived: \"orders / 'a'\"}",
            "x: quotes",
        ),
        (
            "a: {label: A, description: d, format: idr, up_is_good: true, additivity: none,"
            " derived: b}\nb: {label: B, description: d, format: idr, up_is_good: true,"
            " additivity: none, derived: a}",
            "circular definition",
        ),
        (
            "x: {label: X, filtered: {base: orders, where: {has_dog: true}}}",
            "x: unknown dimension has_dog",
        ),
        (
            "aov: {label: A, description: d, format: idr, up_is_good: true, additivity: none,"
            " derived: net_sales / orders}\nx: {label: X, filtered: {base: aov, where: {has_food:"
            " true}}}",
            "x: the base aov must be a sql or filtered metric",
        ),
        ("x: {filtered: {base: orders, where: {has_food: true}}}", "x: missing label"),
        (
            "x: {label: X, description: d, format: idr, up_is_good: true, additivity: none,"
            " derived: net_sales, min_n: {net_sales: 5}}",
            "x: min_n uses net_sales",
        ),
        (
            "x: {label: X, description: d, format: idr, up_is_good: sometimes, additivity: none,"
            " derived: net_sales}",
            "x: up_is_good",
        ),
    ],
)
def test_invalid_metrics(metrics, message):
    with pytest.raises(RegistryError, match="^metrics.yml: " + re.escape(message)):
        _load(metrics)


# --- expressions are read by DuckDB's tokenizer, not by a regular expression ---------------


def _derived(expression: str) -> str:
    """A derived metric `x` (the expression as a JSON string, which YAML reads as it is)."""
    return (
        "x: {label: X, description: d, format: decimal, up_is_good: true, additivity: none,"
        f" derived: {json.dumps(expression)}}}"
    )


@pytest.mark.parametrize(
    ("expression", "inputs"),
    [
        ("net_sales::double / nullif(orders, 0)", {"net_sales", "orders"}),
        ("net_sales::float / nullif(orders::float, 0)", {"net_sales", "orders"}),
        ("cast(net_sales as hugeint) / nullif(orders, 0)", {"net_sales", "orders"}),
        ("net_sales::decimal(18, 2) / nullif(orders::bigint, 0)", {"net_sales", "orders"}),
        ("net_sales::DOUBLE / NULLIF(orders, 0)", {"net_sales", "orders"}),
        ("case when orders > 0 then net_sales::double / orders else null end",
         {"net_sales", "orders"}),
        ("coalesce(net_sales, 0)::integer", {"net_sales"}),
        ("net_sales / nullif(orders, 0) * 1.5e3", {"net_sales", "orders"}),
        ("net_sales between 1 and 2 and not orders is null", {"net_sales", "orders"}),
    ],
)  # fmt: skip
def test_casts_and_type_names_are_not_metrics(expression, inputs):
    assert _load(_derived(expression)).metric("x").inputs == inputs


def test_every_word_that_is_not_syntax_must_be_a_metric():
    # `float` is a type and `nullif` a function, but `revenue` is neither
    with pytest.raises(RegistryError, match=r"x: unknown metric\(s\) revenue$"):
        _load(_derived("revenue::float / nullif(orders, 0)"))
    # a type name used as a value is a name like any other
    with pytest.raises(RegistryError, match=r"x: unknown metric\(s\) hugeint$"):
        _load(_derived("hugeint + orders"))


def test_a_metric_may_be_named_like_an_unreserved_keyword():
    # `share` and `value` are keywords for DuckDB, but it allows them as names
    text = (
        "share: {label: S, description: d, format: pct, up_is_good: true, additivity: none,"
        ' relative: "net_sales / nullif(total.net_sales, 0)"}\n'
        "value: {label: V, description: d, format: decimal, up_is_good: true, additivity: none,"
        " derived: share::double * 2}"
    )
    registry = _load(text)
    assert registry.metric("share").totals == {"net_sales"}
    assert registry.metric("value").inputs == {"share"}


@pytest.mark.parametrize(
    "expression",
    [
        "net_sales / 'a'",
        "net_sales; orders",
        "net_sales -- orders",
        "net_sales /* orders */",
        '"net_sales" / orders',
        "net_sales / 'oops",
    ],
)
def test_quotes_semicolons_and_comments_are_refused(expression):
    with pytest.raises(RegistryError, match=r"^metrics.yml: x: quotes, semicolons and comments"):
        _load(_derived(expression))


def test_expressions_render_with_the_references_replaced():
    rendered = render_expression(
        "net_sales::double / nullif(total.net_sales, 0) * trading_days + cast(a as float)",
        ref=lambda m: f"s.{m}",
        total=lambda m: f"t.{m}",
        trading_days="7",
    )
    assert rendered == "s.net_sales::double / nullif(t.net_sales, 0) * 7 + cast(s.a as float)"
    # the text between the references is kept as written
    assert render_expression("a  +\n b", lambda m: m.upper(), lambda m: m) == "A  +\n B"


@pytest.mark.parametrize(
    "dims, message",
    [
        (
            "w: {label: W, grain: day, models: [fct_orders], domain: all}",
            "w: domain all needs the list of values",
        ),
        ("w: {label: W, grain: day, models: [fct_nothing]}", "w: unknown model(s) fct_nothing"),
        ("w: {label: W, grain: week, models: [fct_orders]}", "w: grain: Input should be"),
        ("w: {label: W, grain: day, models: [fct_orders], bins: [1, '1-2']}", "w: bins"),
        (
            "w: {label: W, grain: day, models: [fct_orders], column: {fct_sales_lines: x}}",
            "w: column names a model that is not in models",
        ),
    ],
)
def test_invalid_dimensions(dims, message):
    with pytest.raises(RegistryError, match="^dimensions.yml: " + re.escape(message)):
        _load(dims=DIMS + dims)


def test_a_dimension_sorted_by_an_unknown_metric():
    dims = DIMS + "w: {label: W, grain: order, models: [fct_orders], sort: {by: revenue}}"
    with pytest.raises(RegistryError, match="w: sort by unknown metric revenue"):
        _load(dims=dims)


AOV = """
aov: {label: A, description: d, format: idr, up_is_good: true, additivity: none,
      derived: "net_sales / nullif(orders, 0)"}
"""


@pytest.mark.parametrize(
    "kpi, message",
    [
        (
            "[{id: a, kpi: net_sales, kind: product, drivers: [orders, aovv]}]",
            "a: unknown metric(s) aovv",
        ),
        (
            "[{id: a, kpi: net_sales, kind: product, drivers: [orders]}]",
            "a: needs at least two drivers",
        ),
        (
            "[{id: a, kpi: net_sales, kind: product, drivers: [orders, -aov]}]",
            "a: a product has no negative drivers",
        ),
        (
            "[{id: a, kpi: aov, kind: sum_over, dimension: payment_type, driver: orders}]",
            "a: the driver orders must be fully additive",
        ),
        (
            "[{id: a, kpi: aov, kind: sum_over, driver: net_sales}]",
            "a: sum_over needs dimension and driver",
        ),
        (
            "[{id: a, kpi: net_sales, kind: product, drivers: [orders, aov], then: b}]",
            "a: dimension, driver and then belong to sum_over",
        ),
        (
            "[{id: a, kpi: net_sales, kind: sum_over, dimension: payment_type, driver: net_sales,"
            " then: b}, {id: b, kpi: aov, kind: product, drivers: [net_sales, orders]}]",
            "a: then must be a product or sum identity of net_sales",
        ),
        (
            "[{id: a, kpi: net_sales, kind: product, drivers: [orders, aov]},"
            " {id: a, kpi: net_sales, kind: product, drivers: [orders, aov]}]",
            "a: the id is used twice",
        ),
        ("[{id: a, kpi: net_sales, kind: ratio, drivers: [orders, aov]}]", "identity 1: kind"),
    ],
)
def test_invalid_identities(kpi, message):
    with pytest.raises(RegistryError, match="^kpi_tree.yml: " + re.escape(message)):
        _load(AOV, kpi=kpi)


def test_a_metric_that_is_not_on_the_model():
    registry = load_registry(
        BASE + "x: {label: X, description: d, format: idr, up_is_good: true, additivity: full,"
        " sql: {fct_sales_lines: sum(net_sales)}}",
        DIMS,
        "[]",
    )
    with pytest.raises(RegistryError, match=r"x is not on fct_orders \(only on fct_sales_lines"):
        registry.plan(["x"], "fct_orders")
    with pytest.raises(RegistryError, match="unknown metric 'y'"):
        registry.plan(["y"], "fct_orders")


def test_a_filtered_metric_exists_only_where_its_dimensions_exist():
    dims = DIMS + "lines_only: {label: L, grain: line, models: [fct_sales_lines]}"
    metrics = (
        "n: {label: N, description: d, format: count, up_is_good: true, additivity: full,"
        " sql: {fct_orders: count(*), fct_sales_lines: count(*)}}\n"
        "x: {label: X, filtered: {base: n, where: {lines_only: [a, b]}}}"
    )
    registry = _load(metrics, dims)
    assert registry.metric("x").models == {"fct_sales_lines"}
    assert registry.aggregate("x", "fct_sales_lines") == (
        "count(*) filter (where lines_only in ('a', 'b'))"
    )
