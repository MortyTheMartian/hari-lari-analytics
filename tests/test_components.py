"""The component schema and loader (plan 7.12): one test per validation rule, each with its
message, parameters and slot overrides, the JSON Schema, and components that run on the
fixture of `test_compiler`."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from hari_lari_analytics.reporting.components import (
    SCHEMA_REF,
    ComponentError,
    component_json_schema,
    deep_merge,
    load_component,
    load_components,
    parameters_in,
    parse_component,
    schema_path,
    schema_text,
    substitute,
)
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.queries import QueryContext, SourceQuery

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

FIXTURES = Path(__file__).parent / "fixtures" / "components"

BASE = """
id: example
title: Example
params: { period: L6W }
query:
  model: fct_orders
  metrics: [net_sales, orders]
  window: $period
  compare: prior
"""


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


def component(text: str = BASE, **changes):
    raw = yaml.safe_load(text)
    return parse_component(deep_merge(raw, changes), "components/example.yml")


def fails(match: str, text: str = BASE, **changes) -> str:
    with pytest.raises(ComponentError, match=match) as error:
        component(text, **changes)
    return str(error.value)


# --- loading the fixture components ---------------------------------------------------------------


def test_the_fixture_components_load():
    components = load_components(FIXTURES)
    assert sorted(components) == [
        "aov_bridge",
        "category_trend",
        "kpi_cards",
        "orders_heatmap",
        "period_cards",
        "top_category_items",
    ]
    assert components["category_trend"].unbound == {"category"}
    assert components["top_category_items"].parameters == {"period", "n"}


def test_every_fixture_file_points_at_the_schema():
    for path in FIXTURES.glob("*.yml"):
        first = path.read_text().splitlines()[0]
        assert first.startswith(SCHEMA_REF.split("=")[0]), path.name


def test_the_packaged_components_load():
    assert isinstance(load_components(), dict)  # the folder fills up from plan 7.15


def test_binding_plans_the_query_and_follows_the_shape():
    bound = load_components(FIXTURES)["aov_bridge"].bind()
    assert bound.query.metrics == ["aov", "items_per_order", "avg_item_price"]
    assert bound.shape == "decomposition"
    assert bound.params == {"period": "L6W"}
    heatmap = load_components(FIXTURES)["orders_heatmap"].bind({"period": "L14D"})
    assert heatmap.shape == "matrix" and heatmap.query.window == "L14D"


def test_a_component_runs(ctx):
    result = load_components(FIXTURES)["aov_bridge"].bind({"period": "WTD"}).run(ctx)
    assert result.meta.shape == "decomposition"
    assert result.df.iloc[-1].effect == 57_500 - 49_000


# --- strict schema --------------------------------------------------------------------------------


def test_an_unknown_key_names_the_file_and_field():
    raw = yaml.safe_load(BASE)
    raw["query"]["metrcis"] = raw["query"].pop("metrics")
    with pytest.raises(ComponentError) as error:
        parse_component(raw, "components/example.yml")
    message = str(error.value)
    assert message.startswith("components/example.yml:")
    assert "query.metrcis: Extra inputs are not permitted" in message
    assert "query.metrics: Field required" in message


def test_an_unknown_top_level_key():
    fails("titel: Extra inputs are not permitted", titel="x")


def test_required_fields():
    fails("title: Field required", "id: example\nquery: {source: notes}")


def test_the_id():
    fails("id: String should match pattern", id="Example")
    path = FIXTURES.parent / "renamed.yml"
    path.write_text((FIXTURES / "kpi_cards.yml").read_text())
    try:
        with pytest.raises(ComponentError, match="kpi_cards must be the file name"):
            load_component(path)
    finally:
        path.unlink()


def test_invalid_yaml(tmp_path):
    path = tmp_path / "broken.yml"
    path.write_text("id: [broken")
    with pytest.raises(ComponentError, match="components/broken.yml: not valid YAML"):
        load_component(path)
    with pytest.raises(ComponentError, match="expected a mapping"):
        parse_component(["a list"], "components/x.yml")


# --- the query, against the registry --------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({"metrics": ["net_salse"]}, "query: unknown metric 'net_salse'"),
        ({"metrics": ["penetration"], "model": "fct_orders", "dimensions": ["adjusted_category"]},
         "query: dimension adjusted_category is not on fct_orders"),
        ({"dimensions": ["weekdya"]}, "query: unknown dimension 'weekdya'"),
        ({"window": "L6X"}, "query: window: unknown window 'L6X'"),
        ({"compare": "yesterday"}, "query.compare: Input should be 'prior'"),
        ({"top": {"n": 3, "by": "orders"}}, "query: top needs a dimension"),
    ],
)  # fmt: skip
def test_the_query_is_checked_against_the_registry(query, message):
    fails(message, query=query)


def test_a_python_source():
    base = "id: example\ntitle: Example\n"
    assert isinstance(component(base + "query: {source: notes}").bind().query, SourceQuery)
    fails("unknown source nope", base + "query: {source: nope}")
    fails(
        "query.options: wrong: Extra inputs", base + "query: {source: notes, options: {wrong: 1}}"
    )


# --- transforms -----------------------------------------------------------------------------------


def test_an_unknown_transform():
    fails(r"transforms.0: unknown transform 'pivt'", transforms=["pivt"])


def test_wrong_transform_options():
    fails("transforms.0: transform sort_limit: by: Field required", transforms=[{"sort_limit": {}}])
    fails("transforms.0: transform sort_limit: n: Input should be greater than or equal to 1",
          transforms=[{"sort_limit": {"by": "orders", "n": 0}}])  # fmt: skip


def test_a_transform_on_the_wrong_shape():
    fails(r"transforms.0: growth_vs_previous does not work on a result of shape single",
          transforms=["growth_vs_previous"])  # fmt: skip


def test_a_requirement_that_cannot_be_added():
    text = BASE.replace("  compare: prior\n", "  dimensions: [payment_type]\n")
    fails("transforms: transform classify needs a comparison", text,
          transforms=[{"classify": {"mode": "lifecycle", "by": "orders"}}])  # fmt: skip


def test_the_shape_follows_the_transforms():
    fails("transforms.1: sort_limit does not work on a result of shape decomposition",
          transforms=[{"decompose": {"identity": "sales_volume_value"}},
                      {"sort_limit": {"by": "effect"}}])  # fmt: skip


def test_the_additivity_check():
    text = BASE.replace("fct_orders", "fct_sales_lines").replace(
        "  compare: prior\n", "  dimensions: [adjusted_category]\n"
    )
    message = fails("transforms: transform cumulative_share: orders counts orders", text,
                    transforms=[{"cumulative_share": {"metric": "orders"}}])  # fmt: skip
    assert "adjusted_category" in message
    fails("aov is not additive", text, transforms=[{"cumulative_share": {"metric": "aov"}}])
    assert component(text, transforms=[{"cumulative_share": {"metric": "net_sales"}}])
    payment = BASE.replace("  compare: prior\n", "  dimensions: [payment_type]\n")
    assert component(payment, transforms=[{"cumulative_share": {"metric": "orders"}}])


# --- parameters -----------------------------------------------------------------------------------


def test_parameters_are_found_and_substituted():
    raw = {"window": "$period", "where": {"a": ["$x", "y"], "b": "$$no"}, "n": "$n"}
    assert parameters_in(raw) == {"period", "x", "n"}
    assert substitute(raw, {"period": "L6W", "x": 1, "n": 5}) == {
        "window": "L6W",
        "where": {"a": [1, "y"], "b": "$$no"},
        "n": 5,
    }
    assert substitute("$period", {}) == "$period"  # unbound stays


def test_a_parameter_without_a_default_is_checked_when_bound(ctx):
    trend = load_components(FIXTURES)["category_trend"]
    with pytest.raises(ComponentError, match=r"\$category are not bound"):
        trend.bind()
    bound = trend.bind({"category": "Coffee"})
    assert bound.query.where == {"adjusted_category": "Coffee"}
    assert bound.query.buckets_before == 1  # growth_vs_previous
    assert bound.run(ctx).meta.shape == "series"


def test_an_unbound_parameter_does_not_hide_other_errors():
    text = BASE.replace("window: $period", "window: $window")
    raw = yaml.safe_load(text)
    raw["params"] = {}
    raw["query"]["top"] = {"n": "$n", "by": "orders"}  # $n is not an int, but unbound: not yet
    raw["query"]["oops"] = 1
    with pytest.raises(ComponentError) as error:
        parse_component(raw, "components/example.yml")
    assert "query.oops" in str(error.value) and "top" not in str(error.value)


def test_a_bound_parameter_of_the_wrong_type():
    raw = yaml.safe_load(BASE)
    raw["query"]["top"] = {"n": "$n", "by": "orders"}
    raw["query"]["dimensions"] = ["payment_type"]
    c = parse_component(raw, "components/example.yml")
    with pytest.raises(ComponentError, match="query.top.n: Input should be a valid integer"):
        c.bind({"n": "five"})
    assert c.bind({"n": 2}).query.top.n == 2


def test_an_unused_default_fails():
    fails("params: perod is not used", params={"period": "L6W", "perod": "L6W"})


def test_parameters_in_title_and_hint():
    c = component(title="{{ category }} in {{ meta.window.key }}", hint="{{ facet.label }}")
    assert c.parameters == {"period", "category"}
    with pytest.raises(ComponentError, match=r"\$category"):
        c.bind()
    assert c.bind({"category": "Coffee"}).params == {"category": "Coffee", "period": "L6W"}
    fails("title: unexpected '}'", title="{{ x }")


def test_report_parameters_the_component_does_not_use_are_ignored():
    assert component().bind({"period": "L14D", "category": "Coffee"}).params == {"period": "L14D"}


# --- overrides of a report slot -------------------------------------------------------------------


def test_deep_merge():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    assert deep_merge(base, {"a": {"c": [3]}, "e": 2}) == {"a": {"b": 1, "c": [3]}, "d": 1, "e": 2}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}  # unchanged


def test_with_overrides_the_component():
    c = load_components(FIXTURES)["top_category_items"]
    bound = c.bind(with_={"query": {"where": {"payment_type": "BCA"}}, "title": "BCA only"})
    assert bound.query.where == {"payment_type": "BCA"} and bound.spec.title == "BCA only"
    assert bound.query.metrics == ["net_sales", "orders"]  # merged, not replaced
    assert bound.spec.facet.values.top == 2


def test_with_can_add_a_parameter():
    c = component()
    bound = c.bind(
        {"category": "Coffee"},
        {"query": {"model": "fct_sales_lines", "where": {"adjusted_category": "$category"}}},
    )
    assert bound.query.where == {"adjusted_category": "Coffee"}  # fmt: skip


def test_with_is_validated():
    c = component()
    with pytest.raises(ComponentError, match=r"\(with the overrides of the slot\): query.metric"):
        c.bind(with_={"query": {"metric": ["orders"]}})
    with pytest.raises(ComponentError, match="cannot change the id"):
        c.bind(with_={"id": "other"})


# --- facets, as far as the loader checks them -----------------------------------------------------


@pytest.mark.parametrize(
    ("facet", "message"),
    [
        ({"by": "period"}, "by period needs a list of windows"),
        ({"by": "period", "values": ["L6W", "L7X"]}, "unknown window 'L7X'"),
        ({"by": "metric", "values": ["aov"]}, "aov is not a metric of the query"),
        ({"by": "metric", "values": {"top": 2, "by": "orders"}}, "not top"),
        ({"by": "metric", "layout": "grid(1x1)"}, "2 facets do not fit into the 1 cells"),
        ({"by": "payment_tpye"}, "by: unknown dimension 'payment_tpye'"),
        ({"by": "adjusted_category"}, "adjusted_category is not on fct_orders"),
        ({"by": "bucket"}, "cannot facet by bucket"),
        ({"by": "payment_type", "layout": "grid(2x2)"}, "values: all can be more values"),
        ({"by": "payment_type", "values": ["BCA", "Cash", "Gopay"], "layout": "grid(1x2)"},
         "3 facets do not fit"),
        ({"by": "payment_type", "values": {"top": 3, "by": "penetration"}},
         "cannot rank by the relative metric penetration"),
        ({"by": "payment_type", "layout": "diagonal"}, "facet.layout: 'diagonal': use row"),
        ({"by": "payment_type", "valeus": "all"}, "facet.valeus: Extra inputs"),
    ],
)  # fmt: skip
def test_facets_are_checked(facet, message):
    fails(message, facet=facet)


def test_by_period_needs_the_period_parameter():
    text = BASE.replace("$period", "L6W").replace("params: { period: L6W }\n", "")
    fails("binds \\$period, which the component does not use", text,
          facet={"by": "period", "values": ["L6W"]})  # fmt: skip


def test_a_dimension_facet_needs_a_compiled_query():
    fails("needs a compiled query", "id: example\ntitle: x\nquery: {source: notes}",
          facet={"by": "payment_type"})  # fmt: skip


# --- the JSON Schema ------------------------------------------------------------------------------


def test_the_schema_file_is_current():
    assert schema_path().read_text() == schema_text(), (
        "run `uv run python -m hari_lari_analytics.reporting.components` to update the schema"
    )


@pytest.fixture(scope="module")
def validator():
    schema = json.loads(schema_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_the_fixture_components_fit_the_schema(validator):
    for path in FIXTURES.glob("*.yml"):
        errors = [e.message for e in validator.iter_errors(yaml.safe_load(path.read_text()))]
        assert not errors, (path.name, errors)


@pytest.mark.parametrize(
    "change",
    [
        {"query": {"metrcis": ["orders"]}},
        {"query": {"metrics": ["net_salse"]}},
        {"query": {"model": "fct_order"}},
        {"query": {"window": "L6X"}},
        {"transforms": ["pivt"]},
        {"transforms": [{"sort_limit": {"by": "orders", "nn": 3}}]},
        {"facet": {"by": "payment_tpye"}},
        {"titel": "x"},
    ],
)
def test_the_schema_catches_typos(validator, change):
    raw = deep_merge(yaml.safe_load(BASE), change)
    assert list(validator.iter_errors(raw)), change


def test_the_schema_accepts_parameters(validator):
    raw = deep_merge(
        yaml.safe_load(BASE),
        {"query": {"dimensions": ["payment_type"], "top": {"n": "$n", "by": "orders"},
                   "metrics": ["orders", "$metric"]},
         "transforms": [{"sort_limit": {"by": "orders", "n": 3}}, "per_trading_day"]},
    )  # fmt: skip
    assert not [e.message for e in validator.iter_errors(raw)]


def test_the_schema_lists_the_registry():
    schema = component_json_schema()
    query = schema["$defs"]["Query"]["properties"]
    assert "net_sales" in query["metrics"]["anyOf"][0]["items"]["anyOf"][0]["enum"]
    assert "fct_orders" in query["model"]["anyOf"][0]["enum"]
    names = schema["properties"]["transforms"]["items"]["anyOf"][0]["enum"]
    assert {"decompose", "pivot", "sort_limit"} <= set(names)
