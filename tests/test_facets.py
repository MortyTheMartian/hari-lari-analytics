"""The facet engine (plan 7.13): by dimension value (all, a list, the top N decided once over the
component's window), by period and by metric, on the fixture of `test_compiler`:

    R001 Mon 05 Oct BCA    Latte (Coffee) 40,000 + Focaccia (Snacks (Focaccia)) 28,000
    R002 Mon 05 Oct Cash   Latte (Coffee) 40,000 - 10,000 FOUNDER25 discount
    R003 Tue 06 Oct Gopay  Cappucino (Coffee) 40,000, fully refunded: not an order
    R004 Mon 12 Oct BCA    2 x Nasi Campur (Mains) 35,000 + Mystery Cake (Cakes) 30,000
    R005 Tue 13 Oct BCA    Custom Amount 15,000

and on the run of 2026-11-22.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hari_lari_analytics.reporting.compiler import Query, run_query
from hari_lari_analytics.reporting.components import load_components, parse_component
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.facets import FacetSpec, run_facets
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.registry import default_registry

from .api_fixture import api_tables
from .test_compiler import real, real_run  # noqa: F401  (real_run: a fixture)
from .test_models import LAST_SALE, SEEDS

FIXTURES = Path(__file__).parent / "fixtures" / "components"
COMPONENTS = load_components(FIXTURES)
REG = default_registry()


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


def make(facet: dict, model: str = "fct_sales_lines", **query):
    raw = {
        "id": "example",
        "title": "Example",
        "query": {"model": model, "metrics": ["net_sales", "orders"], "window": "full", **query},
        "facet": facet,
    }
    return parse_component(raw, "components/example.yml")


def totals(facets) -> dict:
    return {f.key: f.result.df.iloc[-1].net_sales for f in facets}


def test_without_a_facet_one_cell(ctx):
    facets = run_facets(COMPONENTS["aov_bridge"], ctx, {"period": "WTD"})
    assert len(facets) == 1 and facets[0].key is None and facets[0].label is None
    assert facets[0].result.meta.shape == "decomposition"


def test_by_metric_hands_one_metric_to_each_cell(ctx):
    facets = run_facets(COMPONENTS["kpi_cards"], ctx)
    assert [f.metric for f in facets] == ["net_sales", "orders", "aov", "items_sold"]
    assert [f.label for f in facets] == ["Net Sales", "Orders", "Avg Order Value", "Items Sold"]
    assert all(f.result is facets[0].result for f in facets)  # one query for all cells


def test_by_metric_with_a_list(ctx):
    facets = run_facets(make({"by": "metric", "values": ["orders"]}), ctx)
    assert [f.key for f in facets] == ["orders"]


def test_by_period_binds_each_window(ctx):
    facets = run_facets(COMPONENTS["period_cards"], ctx)
    assert [f.key for f in facets] == ["WTD", "L14D", "full"]
    assert [f.label for f in facets] == ["Week to date", "Last 14 days", "Full history"]
    assert [f.result.meta.window.key for f in facets] == ["WTD", "L14D", "full"]
    assert [f.result.df.iloc[0].net_sales for f in facets] == [115_000, 213_000, 213_000]


def test_by_dimension_top_n_over_the_components_window(ctx):
    # full: Coffee 70,000 and Mains 70,000 (ties by name), then Cakes 30,000
    facets = run_facets(COMPONENTS["top_category_items"], ctx)
    assert [f.key for f in facets] == ["Coffee", "Mains"]
    assert [f.label for f in facets] == ["Coffee", "Mains"]
    assert totals(facets) == {"Coffee": 70_000, "Mains": 70_000}
    coffee = facets[0].result
    assert coffee.rows["adjusted_item"].tolist() == ["Latte", "Cappucino"]
    assert coffee.meta.window.key == "full"
    # WTD: Mains 70,000, Cakes 30,000, Custom Amount 15,000
    facets = run_facets(COMPONENTS["top_category_items"], ctx, {"period": "WTD", "n": 2})
    assert totals(facets) == {"Mains": 70_000, "Cakes": 30_000}


def test_the_top_n_respects_the_components_filters(ctx):
    # BCA only: Mains 70,000, Coffee 40,000 (R001), Cakes 30,000, Snacks 28,000
    facets = run_facets(
        COMPONENTS["top_category_items"], ctx, with_={"query": {"where": {"payment_type": "BCA"}}}
    )
    assert totals(facets) == {"Mains": 70_000, "Coffee": 40_000}


def test_by_dimension_all_in_the_dimensions_order(ctx):
    facets = run_facets(make({"by": "payment_type"}, model="fct_orders"), ctx)
    assert [f.key for f in facets] == ["BCA", "Cash", "Gopay"]  # by net sales
    assert totals(facets) == {"BCA": 183_000, "Cash": 30_000, "Gopay": 0}


def test_by_dimension_with_a_list_and_an_empty_value(ctx):
    facets = run_facets(make({"by": "discount_code", "values": ["FOUNDER25", None]}), ctx)
    assert [f.label for f in facets] == ["FOUNDER25", "No discount"]
    assert totals(facets) == {"FOUNDER25": 30_000, None: 183_000}


def test_a_boolean_dimension_is_labelled(ctx):
    facets = run_facets(make({"by": "has_food"}, model="fct_orders"), ctx)
    assert {f.label for f in facets} == {"With food", "Without food"}
    assert sum(totals(facets).values()) == 213_000


def test_no_sales_no_facets(ctx):
    facets = run_facets(make({"by": "payment_type"}, where={"payment_type": "Nope"}), ctx)
    assert facets == []


def test_a_slot_parameter_reaches_the_facet(ctx):
    raw = yaml.safe_load((FIXTURES / "top_category_items.yml").read_text())
    component = parse_component(raw, "components/top_category_items.yml")
    assert len(run_facets(component, ctx, {"n": 1})) == 1


def test_the_layout():
    assert FacetSpec(by="metric", layout="grid(2x3)").grid == (2, 3)
    assert FacetSpec(by="metric", layout="grid(2 x 3)").cells == 6
    assert FacetSpec(by="metric").cells is None


@real
def test_top_categories_on_the_real_run(real_run):  # noqa: F811
    con, calendar, _ = real_run
    ctx = QueryContext(con, REG, calendar)
    component = make(
        {"by": "adjusted_category", "values": {"top": 5, "by": "net_sales"}, "layout": "grid(2x3)"},
        dimensions=["bucket"],
        window="L6W",
        compare="prior",
        totals=True,
    )
    facets = run_facets(component, ctx)
    categories = run_query(
        con,
        Query(model="fct_sales_lines", dimensions=["adjusted_category"], metrics=["net_sales"],
              window="L6W", compare="prior"),
        REG,
        calendar,
    )  # fmt: skip
    expected = categories.rows.head(5)
    assert [f.key for f in facets] == expected["adjusted_category"].tolist()
    for facet, (_, row) in zip(facets, expected.iterrows(), strict=True):
        total = facet.result.total
        assert total.net_sales == row.net_sales and total.prior_net_sales == row.prior_net_sales
        assert facet.result.meta.shape == "series" and len(facet.result.meta.buckets) == 6
