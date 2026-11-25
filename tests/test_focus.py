"""Relative metrics of one member (plan 7A.1 and 7A.2): a category's penetration, share and
contributive AOV must be the same in the categories table, in a query focused on the category,
in a facet by category and in a weekly series of the category. `where` narrows the universe and
`focus` keeps only some groups of it. On the fixture of `test_compiler`:

    R001 Mon 05 Oct BCA    Latte (Coffee) 40,000 + Focaccia (Snacks (Focaccia)) 28,000
    R002 Mon 05 Oct Cash   Latte (Coffee) 40,000 - 10,000 FOUNDER25 discount
    R003 Tue 06 Oct Gopay  Cappucino (Coffee) 40,000, fully refunded: not an order
    R004 Mon 12 Oct BCA    2 x Nasi Campur (Mains) 35,000 + Mystery Cake (Cakes) 30,000
    R005 Tue 13 Oct BCA    Custom Amount 15,000

Coffee over the full history: 70,000 of 213,000 net sales, in 2 of the 4 orders.
"""

from __future__ import annotations

import pytest

from hari_lari_analytics.reporting.compiler import Query, QueryError, run_query
from hari_lari_analytics.reporting.components import ComponentError, parse_component
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.registry import default_registry

from .api_fixture import api_tables
from .test_compiler import real, real_run, same  # noqa: F401  (real_run: a fixture)
from .test_models import LAST_SALE, SEEDS

REG = default_registry()
RELATIVE = ["penetration", "share", "contributive_aov"]
METRICS = ["net_sales", "orders", *RELATIVE]
COMPARED = [*METRICS, *(f"prior_{m}" for m in METRICS)]


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


def q(ctx, **query):
    return run_query(ctx.con, Query(**query), REG, ctx.calendar)


def member(ctx, category: str, **query):
    """One category's row, as a query focused on it."""
    return q(ctx, model="fct_sales_lines", metrics=METRICS,
             focus={"adjusted_category": category}, **query)  # fmt: skip


def table_row(ctx, category: str, **query):
    r = q(ctx, model="fct_sales_lines", dimensions=["adjusted_category"], metrics=METRICS,
          keep_prior_only=query.get("compare", "none") != "none", **query)  # fmt: skip
    return r.rows.set_index("adjusted_category").loc[category]


def example(query: dict, facet: dict | None = None):
    raw = {"id": "example", "title": "Example", "query": {"model": "fct_sales_lines", **query}}
    if facet:
        raw["facet"] = facet
    return parse_component(raw, "components/example.yml")


def faceted(ctx, category: str, **query):
    component = example(
        {"metrics": METRICS, **query}, {"by": "adjusted_category", "values": [category]}
    )
    facets = run_facets(component, ctx)
    assert [f.key for f in facets] == [category]
    return facets[0].result


def test_the_table_row_by_hand(ctx):
    coffee = table_row(ctx, "Coffee", window="full")
    assert (coffee.net_sales, coffee.orders) == (70_000, 2)
    assert coffee.penetration == 0.5
    assert coffee.share == pytest.approx(70_000 / 213_000)
    assert coffee.contributive_aov == 70_000 / 4


@pytest.mark.parametrize("category", ["Coffee", "Mains", "Custom Amount"])
def test_restricted_to_one_category(ctx, category):
    expected = table_row(ctx, category, window="full")
    row = member(ctx, category, window="full").df.iloc[0]
    assert same(row[METRICS], expected[METRICS])


def test_restricted_with_a_comparison(ctx):
    # WTD against the week before: Coffee sold only in the prior week
    expected = table_row(ctx, "Coffee", window="WTD", compare="prior")
    row = member(ctx, "Coffee", window="WTD", compare="prior").df.iloc[0]
    assert same(row[COMPARED], expected[COMPARED])
    assert row.prior_penetration == 1.0  # R001 and R002 were the only orders that week


def test_restricted_under_another_filter(ctx):
    # BCA only: R001, R004, R005; Coffee in R001
    expected = table_row(ctx, "Coffee", window="full", where={"payment_type": "BCA"})
    row = member(ctx, "Coffee", window="full", where={"payment_type": "BCA"}).df.iloc[0]
    assert same(row[METRICS], expected[METRICS])
    assert row.penetration == pytest.approx(1 / 3)


def test_a_where_on_the_member_is_refused_with_a_relative_metric(ctx):
    with pytest.raises(QueryError, match=r"where: adjusted_category .* use focus"):
        q(ctx, model="fct_sales_lines", metrics=["net_sales", "penetration"], window="full",
          where={"adjusted_category": "Coffee"})  # fmt: skip
    with pytest.raises(QueryError, match="use focus"):  # a series has no group either
        q(ctx, model="fct_sales_lines", dimensions=["bucket"], metrics=["share"], window="full",
          where={"payment_type": "BCA"})  # fmt: skip
    with pytest.raises(ComponentError, match="use focus"):
        example({"metrics": ["penetration"], "window": "full",
                  "where": {"adjusted_category": "Coffee"}})  # fmt: skip
    # without a relative metric, a where is fine
    r = q(ctx, model="fct_sales_lines", metrics=["net_sales", "category_aov"], window="full",
          where={"adjusted_category": "Coffee"})  # fmt: skip
    assert (r.df.iloc[0].net_sales, r.df.iloc[0].category_aov) == (70_000, 35_000)


def test_grouped_under_a_where_the_values_are_within_the_filter(ctx):
    # Coffee: Latte-Hot in R001 (40,000), Latte-Cold in R002 (30,000); Cappucino nets 0
    r = q(ctx, model="fct_sales_lines", dimensions=["adjusted_item_variant"],
          metrics=["net_sales", "share", "penetration"], window="full",
          where={"adjusted_category": "Coffee"}, totals=True)  # fmt: skip
    latte = r.rows.set_index("adjusted_item_variant").loc["Latte-Hot"]
    assert latte.share == pytest.approx(4 / 7)  # of Coffee's sales
    assert latte.penetration == 0.5  # of Coffee's orders
    assert r.rows["share"].sum() == pytest.approx(1)
    assert (r.total.net_sales, r.total.share) == (70_000, 1)


def test_the_total_row_is_that_of_the_focus(ctx):
    r = q(ctx, model="fct_sales_lines", dimensions=["adjusted_item_variant"], metrics=METRICS,
          window="full", focus={"adjusted_category": "Coffee"}, totals=True)  # fmt: skip
    latte = r.rows.set_index("adjusted_item_variant").loc["Latte-Hot"]
    assert latte.share == pytest.approx(40_000 / 213_000)  # of everything
    assert latte.penetration == 0.25
    coffee = table_row(ctx, "Coffee", window="full")
    assert same(r.total[METRICS], coffee[METRICS])  # the total is Coffee as a whole
    assert r.meta.trading_days == 3  # a focus on a category does not change the trading days


def test_a_focus_on_several_values_is_their_slice(ctx):
    # Coffee and Mains: R001, R002, R004 of 4 orders, 140,000 of 213,000
    row = member(ctx, ["Coffee", "Mains"], window="full").df.iloc[0]
    assert (row.net_sales, row.orders, row.penetration) == (140_000, 3, 0.75)
    assert row.share == pytest.approx(140_000 / 213_000)
    assert row.contributive_aov == 140_000 / 4


def test_top_ranks_within_the_focus(ctx):
    r = q(ctx, model="fct_sales_lines", dimensions=["adjusted_category"], metrics=METRICS,
          window="full", focus={"adjusted_category": ["Cakes", "Coffee", "Mains"]},
          top={"n": 1, "by": "net_sales", "other": True}, totals=True)  # fmt: skip
    # Coffee and Mains tie at 70,000 (the name decides); Other holds Mains and Cakes only
    assert r.df["adjusted_category"].tolist()[:2] == ["Coffee", "Other (2)"]
    other = r.df.iloc[1]
    assert (other.net_sales, other.orders) == (100_000, 1)
    assert other.share == pytest.approx(100_000 / 213_000) and other.penetration == 0.25
    assert r.total.net_sales == 170_000


def test_a_focus_on_a_weekday_counts_its_trading_days(ctx):
    # Tuesdays: 6 Oct (R003, refunded: no trading day) and 13 Oct (R005, 15,000)
    r = q(ctx, model="fct_orders", metrics=["net_sales_per_trading_day", "net_sales"],
          window="full", focus={"weekday": 2})  # fmt: skip
    row = r.df.iloc[0]
    assert (row.net_sales, row.net_sales_per_trading_day, r.meta.trading_days) == (
        15_000, 15_000, 1
    )  # fmt: skip
    share = q(ctx, model="fct_orders", metrics=["share"], window="full", focus={"weekday": 2})
    assert share.df.iloc[0].share == pytest.approx(15_000 / 213_000)


def test_a_focus_with_buckets_before_the_window(ctx):
    # WTD by day with the day before: Sunday 11 Oct (no sales), Mon 12 and Tue 13 Oct
    r = q(ctx, model="fct_sales_lines", dimensions=["bucket"], metrics=["net_sales", "share"],
          window="WTD", buckets_before=1, focus={"adjusted_category": "Mains"})  # fmt: skip
    assert r.df["net_sales"].tolist() == [0, 70_000, 0]
    assert r.df["share"].tolist()[1:] == [70_000 / 100_000, 0]  # of each day's sales


def test_a_facet_within_its_value(ctx):
    component = example(
        {"dimensions": ["adjusted_item_variant"], "metrics": ["net_sales", "share"],
         "window": "full", "totals": True},
        {"by": "adjusted_category", "values": ["Coffee", "Mains"], "within": True},
    )  # fmt: skip
    for facet in run_facets(component, ctx):
        assert facet.result.rows["share"].sum() == pytest.approx(1)  # a pie of the category
        assert facet.result.total.share == 1
    focused = run_facets(component, ctx, with_={"facet": {"within": False}})
    assert focused[0].result.total.share == pytest.approx(70_000 / 213_000)


def test_a_facet_within_its_value_is_refused_without_a_group(ctx):
    with pytest.raises(ComponentError, match="facet: the query of one facet: .*use focus"):
        example({"metrics": ["share"], "window": "full"},
                {"by": "adjusted_category", "within": True})  # fmt: skip


def test_a_facet_by_category(ctx):
    expected = table_row(ctx, "Coffee", window="full")
    row = faceted(ctx, "Coffee", window="full").df.iloc[0]
    assert same(row[METRICS], expected[METRICS])


def test_a_weekly_series_of_one_category(ctx):
    grid = q(ctx, model="fct_sales_lines", dimensions=["bucket", "adjusted_category"],
             metrics=METRICS, window="full", grain="week")  # fmt: skip
    expected = grid.rows[grid.rows["adjusted_category"] == "Coffee"].set_index("bucket")
    series = member(ctx, "Coffee", dimensions=["bucket"], window="full", grain="week")
    rows = series.rows.set_index("bucket")
    assert len(rows) == 2  # the weeks of 5 and 12 Oct; Coffee sold only in the first
    assert same(rows.loc[expected.index, METRICS], expected[METRICS])
    assert rows.iloc[1][["net_sales", "orders", "penetration"]].tolist() == [0, 0, 0]


@real
def test_the_top_category_on_the_real_run(real_run):  # noqa: F811
    con, calendar, _ = real_run
    ctx = QueryContext(con, REG, calendar)
    table = q(ctx, model="fct_sales_lines", dimensions=["adjusted_category"], metrics=METRICS,
              window="L6W", compare="prior")  # fmt: skip
    top = table.rows.iloc[0]
    category = top.adjusted_category
    assert round(top.penetration, 3) == 0.532
    row = member(ctx, category, window="L6W", compare="prior").df.iloc[0]
    assert same(row[COMPARED], top[COMPARED])
    facet = faceted(ctx, category, window="L6W", compare="prior").df.iloc[0]
    assert same(facet[COMPARED], top[COMPARED])
