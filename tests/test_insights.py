"""The insight engine (plan 8.3): the rules of insights.yml, each rule type with its promises
(no sentence below `min_n`, none without a valid comparison), the loader's check of a
component's rules, and collecting, ranking and de-duplicating the sentences.

The fixture has no valid comparison, so the comparing rules run on fixture results whose
comparison columns and counts are filled in by `compared`."""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest
import yaml

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.analysis.result import Result, ResultMeta
from hari_lari_analytics.presentation import insights, visuals
from hari_lari_analytics.presentation.components import ComponentError, parse_component
from hari_lari_analytics.presentation.facets import Facet, run_facets
from hari_lari_analytics.presentation.insights import (
    Insight,
    InsightContext,
    InsightError,
    collect,
    component_insights,
    default_rules,
    evaluate,
    parse_rules,
)
from hari_lari_analytics.warehouse.db import connect

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

BIG, SMALL = 500, 5  # order counts above and below the sample-size rule


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


@pytest.fixture(scope="module")
def ictx(ctx):
    return InsightContext(ctx.calendar)


def component(text: str):
    raw = {"id": "example", "title": "Example", **yaml.safe_load(text)}
    return parse_component(raw, "components/example.yml")


def run(ctx, text: str) -> Result:
    return run_facets(component(text), ctx)[0].result


def compared(result: Result, n: int = BIG, **columns) -> Result:
    """The result with a valid comparison, these columns set and `n` orders on both sides."""
    df = result.df.copy()
    for name, values in columns.items():
        df[name] = values
    for c in [c for c in df.columns if c.startswith(("n_", "prior_n_"))]:
        df[c] = n
    window = dataclasses.replace(result.meta.window, compare_ok=True)
    return result.replace(df, window=window)


def rule(name: str):
    return default_rules()[name]


# --- the rules of insights.yml ---------------------------------------------------------------


def test_the_packaged_rules_load():
    rules = default_rules()
    assert {"top_drivers", "movers", "best_bucket", "data_notes"} <= set(rules)
    assert {r.type.name for r in rules.values()} == set(insights.RULE_TYPES)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"type": "nope", "text": "x"}, "unknown rule type 'nope'"),
        ({"type": "movers"}, "text: the sentence is missing"),
        ({"type": "movers", "text": "{{ kpi.label }}"}, "kpi is not a variable of a movers"),
        ({"type": "movers", "text": "{{ row.label"}, "text: unexpected end of template"),
        ({"type": "movers", "top": 0, "text": "x"}, "top: Input should be greater than"),
        ({"type": "movers", "colour": "red", "text": "x"}, "colour: Extra inputs"),
        ({"type": "movers", "weight": 0, "text": "x"}, "weight: a positive number"),
        ({"type": "threshold", "metric": "aov", "text": "x"}, "exactly one of above and below"),
    ],
)
def test_a_bad_rule_fails_with_its_name(body, match):
    with pytest.raises(InsightError, match=match) as error:
        parse_rules({"bad": body})
    assert str(error.value).startswith("insights.yml: bad: ")


# --- movers ----------------------------------------------------------------------------------

BREAKDOWN = """
query: {model: fct_sales_lines, dimensions: [adjusted_category], metrics: [net_sales, penetration],
        window: L14D, compare: prior, totals: true}
"""


def breakdown(ctx, n=BIG) -> Result:
    result = run(ctx, BREAKDOWN)
    net = result.df["net_sales"]
    rows = len(result.df)
    return compared(
        result,
        n,
        prior_net_sales=net * 0.5,
        net_sales_delta=net * 0.5,
        net_sales_change=1.0,
        prior_penetration=result.df["penetration"] - 0.1,
        penetration_delta=[0.1 + i / 100 for i in range(rows)],
        penetration_change=0.2,
    )


def test_movers_name_the_largest_moves(ctx, ictx):
    said = evaluate(rule("movers"), breakdown(ctx), ictx)
    assert len(said) == 2
    assert said[0].score >= said[1].score
    assert all(s.tone == "good" for s in said)
    assert all(" vs " in s.text for s in said)


def test_no_mover_below_the_sample_size_rule(ctx, ictx):
    assert evaluate(rule("movers"), breakdown(ctx, SMALL), ictx) == []


def test_no_mover_without_a_valid_comparison(ctx, ictx):
    result = breakdown(ctx)
    window = dataclasses.replace(result.meta.window, compare_ok=False)
    assert evaluate(rule("movers"), result.replace(window=window), ictx) == []


def test_a_component_changes_a_rules_options(ctx, ictx):
    result = breakdown(ctx)
    facets = [Facet(None, None, result)]
    refs = [{"movers": {"metrics": ["penetration"], "top": 1}}]
    (said,) = component_insights("example", refs, facets, ictx)
    assert said.key[1] == "penetration"
    assert " pp " in said.text
    assert said.component == "example"


def test_the_movers_threshold_is_by_format(ctx, ictx):
    rules = parse_rules(
        {"m": {"type": "movers", "min_abs_delta": {"pct": 0.5, "idr": 1e12}, "text": "x"}}
    )
    assert evaluate(rules["m"], breakdown(ctx), ictx) == []


# --- drivers ---------------------------------------------------------------------------------

BRIDGE = """
query: {model: fct_orders, metrics: [aov], window: L14D, compare: prior}
transforms: [{decompose: {identity: aov_basket}}]
"""


def bridge(ctx, n=BIG, status=None) -> Result:
    """AOV 60,000 to 62,750: items per order +3,000, price per item -250."""
    result = run(ctx, BRIDGE)
    assert list(result.df["metric"]) == ["items_per_order", "avg_item_price", "aov"]
    return compared(
        result,
        n,
        prior=[2.0, 50_000.0, 60_000.0],
        current=[2.2, 50_000.0, 62_750.0],
        delta=[0.2, 0.0, 2750.0],
        change=[0.1, 0.0, 2750 / 60_000],
        effect=[3000.0, -250.0, 2750.0],
        share_of_change=[1.1, -0.1, 1.0],
        status=[status, None, None],
    )


def test_drivers_name_the_largest_effects(ctx, ictx):
    (said,) = evaluate(rule("top_drivers"), bridge(ctx), ictx)
    assert said.text.startswith("Avg Order Value +4.6% vs ")
    assert "items per order +3,000" in said.text
    assert "avg net price" not in said.text  # its share of the change is below 0.2
    assert said.tone == "good"
    assert said.score == pytest.approx(2750 / 60_000)


def test_no_drivers_when_the_change_is_not_split_or_small(ctx, ictx):
    assert evaluate(rule("top_drivers"), bridge(ctx, status="undefined"), ictx) == []
    assert evaluate(rule("top_drivers"), bridge(ctx, n=SMALL), ictx) == []


# --- record ----------------------------------------------------------------------------------


def trend(ctx, window="full", grain="day", n=BIG, last=None) -> Result:
    result = run(
        ctx,
        f"query: {{model: fct_orders, dimensions: [bucket], metrics: [net_sales, discounts],"
        f" window: {window}, grain: {grain}}}",
    )
    df = result.df.copy()
    values = [float(i + 1) * 1000 for i in range(len(df))]
    if last is not None:
        values[-1] = last
    df["net_sales"] = values
    df["discounts"] = values
    df["trading_days"] = 1
    df["n_orders"] = n
    return result.replace(df)


def test_a_record_when_the_latest_bucket_is_the_best(ctx, ictx):
    (said,) = evaluate(rule("best_bucket"), trend(ctx), ictx)
    assert said.text.startswith("Best day since opening: ")
    assert "previous best" in said.text
    assert said.tone == "good"


def test_no_record_when_the_latest_bucket_is_not_the_best(ctx, ictx):
    assert evaluate(rule("best_bucket"), trend(ctx, last=1.0), ictx) == []
    assert evaluate(rule("best_bucket"), trend(ctx, n=SMALL), ictx) == []


def test_since_opening_only_from_the_first_sale(ctx, ictx):
    result = trend(ctx, window="last_n_days(5)")
    assert result.meta.buckets[0].start > ctx.calendar.first_sale
    (said,) = evaluate(rule("best_bucket"), result, ictx)
    assert said.text.startswith("Best day in the last 5 days: ")


def test_a_record_of_a_down_is_good_metric_is_the_lowest(ctx, ictx):
    rules = parse_rules({"low": {"type": "record", "metric": "discounts", "text": "{{ value }}"}})
    assert evaluate(rules["low"], trend(ctx), ictx) == []
    (said,) = evaluate(rules["low"], trend(ctx, last=1.0), ictx)
    assert said.text == "1"


def test_a_partial_last_bucket_does_not_count(ctx, ictx):
    result = trend(ctx, grain="week")
    last = result.meta.buckets[-1]
    assert last.end > result.meta.window.current.end  # the anchor is a Tuesday
    df = result.df.copy()
    df["net_sales"] = [1000.0] * (len(df) - 1) + [9e9]
    assert evaluate(rule("best_bucket"), result.replace(df), ictx) == []


# --- threshold and notes ---------------------------------------------------------------------


def test_a_threshold_crossing(ctx, ictx):
    query = "{model: fct_sales_lines, metrics: [discount_rate], window: L14D, compare: prior}"
    result = run(ctx, f"query: {query}")
    crossed = compared(result, discount_rate=0.15, prior_discount_rate=0.05)
    (said,) = evaluate(rule("discount_rate_high"), crossed, ictx)
    assert said.text.startswith("Discount rate rose above 10.0%: 15.0%, from 5.0%")
    assert said.tone == "bad"
    stayed = compared(result, discount_rate=0.15, prior_discount_rate=0.12)
    assert evaluate(rule("discount_rate_high"), stayed, ictx) == []


def test_data_notes(ictx):
    df = pd.DataFrame(
        {"kind": ["source", "check", "extraction"], "note": ["run x", "a check failed", "slow"]}
    )
    result = Result(df, ResultMeta(shape="rows", window=None, empty=False))
    said = evaluate(rule("data_notes"), result, ictx)
    assert [s.text for s in said] == ["A check failed", "Slow"]


def test_an_empty_window_says_nothing(ctx, ictx):
    result = run(ctx, BREAKDOWN.replace("window: L14D", "window: L14D, where: {item: nope}"))
    assert result.meta.empty
    assert evaluate(rule("movers"), result, ictx) == []


# --- collect ---------------------------------------------------------------------------------


def test_collect_keeps_the_best_per_key_and_ranks():
    a = Insight("Coffee up", 0.2, "movers", (None, "orders", "Coffee"), component="one")
    b = Insight("Coffee up a lot", 0.5, "movers", (None, "orders", "Coffee"), component="two")
    c = Insight("Best week", 0.3, "best_bucket", (None, "net_sales", "record"))
    assert collect([a, b, c]) == [b, c]
    assert collect([a, b, c], top=1) == [b]


def test_the_insights_visual_and_its_text(ctx):
    said = [Insight("Best week since opening", 0.3, "best_bucket", ("k",), "good")]
    result = run(ctx, "query: {model: fct_orders, metrics: [orders], window: L14D}")
    text = visuals.plain_text("insights", {}, result, visuals.DrawContext(insights=said))
    assert text == ["Best week since opening"]
    assert visuals.plain_text("insights", {}, result) == ["Nothing stands out"]


# --- the loader's check ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("insights_yaml", "match"),
    [
        ("[nope]", "unknown rule 'nope'"),
        ("[top_drivers]", "a drivers rule reads a result of shape decomposition, not rows"),
        ("[{movers: {metrics: [aov]}}]", "metrics: aov is not a metric of the query"),
        ("[{movers: {top: 0}}]", "movers: top: Input should be greater than"),
        ("[{movers: {}, best_bucket: {}}]", "a rule name, or a mapping of one rule name"),
    ],
)
def test_the_loader_checks_the_rules(insights_yaml, match):
    with pytest.raises(ComponentError, match=match) as error:
        component(BREAKDOWN + f"insights: {insights_yaml}")
    assert str(error.value).startswith("components/example.yml: insights: ")


def test_a_comparing_rule_needs_compare():
    text = BREAKDOWN.replace("compare: prior, ", "") + "insights: [movers]"
    with pytest.raises(ComponentError, match="movers: needs a comparison"):
        component(text)


def test_a_record_rule_on_a_series_loads():
    comp = component(
        "query: {model: fct_orders, dimensions: [bucket], metrics: [orders], window: L6W}\n"
        "insights: [best_bucket, {best_bucket: {metric: orders, weight: 2}}]"
    )
    assert len(comp.bind().spec.insights) == 2
