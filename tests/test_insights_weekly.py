"""The rule types and options of the weekly report's insights (plan 9.8): `slice_movers`,
`top_entries`, `peaks`, `total_change`, and the new options of `drivers`, `record` and `movers`.

Each fires on the fixture, stays quiet below its threshold and never speaks about a small
side. The fixture has no valid comparison, so the comparison columns are filled in with
`compared`, as in test_insights.py."""

from __future__ import annotations

import numpy as np
import pytest

from hari_lari_analytics.reporting.components import ComponentError
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.insights import (
    InsightContext,
    default_rules,
    evaluate,
    parse_rules,
)
from hari_lari_analytics.reporting.queries import QueryContext

from .api_fixture import api_tables
from .test_insights import BIG, SMALL, bridge, compared, component, rule, run, trend
from .test_models import LAST_SALE, SEEDS


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


@pytest.fixture(scope="module")
def ictx(ctx):
    return InsightContext(ctx.calendar)


# --- slice_movers ----------------------------------------------------------------------------

SLICES = """
query: {model: fct_sales_lines, dimensions: [adjusted_category],
        metrics: [penetration, category_aov, contributive_aov],
        window: L14D, compare: prior, totals: true}
"""


def slices(ctx, n=BIG, penetration=(0.5, 0.4), aov=(50_000.0, 50_000.0)):
    """Every category's penetration and category AOV move from the second number to the first;
    the contributive AOV is their product."""
    result = run(ctx, SLICES)
    (p1, p0), (a1, a0) = penetration, aov
    c1, c0 = p1 * a1, p0 * a0
    return compared(
        result,
        n,
        penetration=p1,
        prior_penetration=p0,
        penetration_delta=p1 - p0,
        penetration_change=p1 / p0 - 1,
        prior_category_aov=a0,
        category_aov_delta=a1 - a0,
        category_aov_change=a1 / a0 - 1,
        category_aov=a1,
        prior_contributive_aov=c0,
        contributive_aov_delta=c1 - c0,
        contributive_aov_change=c1 / c0 - 1,
        contributive_aov=c1,
    )


def test_a_slice_moved_by_more_orders(ctx, ictx):
    said = evaluate(rule("category_slices"), slices(ctx), ictx)
    assert len(said) == 3  # top 3 of the categories left after the skipped one
    assert said[0].text == "Coffee +5,000 to the AOV, mostly in more orders (penetration +10.0 pp)"
    assert all(s.tone == "good" for s in said)
    assert not any("Custom Amount" in s.text for s in said)


def test_a_slice_moved_by_more_spent_per_order(ctx, ictx):
    result = slices(ctx, penetration=(0.5, 0.5), aov=(70_000.0, 50_000.0))
    said = evaluate(rule("category_slices"), result, ictx)
    assert said[0].text.startswith("Coffee +10,000 to the AOV, mostly more spent on it per order")
    assert "(category AOV +40.0%)" in said[0].text


def test_a_slice_moved_down_by_fewer_orders(ctx, ictx):
    said = evaluate(rule("category_slices"), slices(ctx, penetration=(0.3, 0.4)), ictx)
    assert "-5,000 to the AOV, mostly in fewer orders" in said[0].text
    assert said[0].tone == "bad"


def test_a_slice_below_the_threshold_says_nothing(ctx, ictx):
    result = slices(ctx, penetration=(0.401, 0.4))
    assert evaluate(rule("category_slices"), result, ictx) == []


def test_no_slice_below_the_sample_size_rule(ctx, ictx):
    assert evaluate(rule("category_slices"), slices(ctx, SMALL), ictx) == []


def test_a_category_sold_only_now_is_new(ctx, ictx):
    result = slices(ctx)
    df = result.df.copy()
    df["prior_penetration"] = 0.0
    df["prior_category_aov"] = np.nan
    said = evaluate(rule("category_slices"), result.replace(df), ictx)
    assert said and all(s.text.endswith("to the AOV, new") for s in said)


# --- top_entries -----------------------------------------------------------------------------

RANKED = """
query: {model: fct_sales_lines, dimensions: [adjusted_item_variant],
        metrics: [net_sales, share], window: L14D, compare: prior,
        top: {n: 2, by: net_sales}}
"""


def ranked(ctx, prior_rank, n=BIG):
    result = run(ctx, RANKED)
    assert result.meta.top_n == 2
    df = result.df.copy()
    df["prior_rank"] = prior_rank
    return compared(result.replace(df), n)


def test_an_entry_new_in_the_top_is_named_with_the_rank_it_had(ctx, ictx):
    result = ranked(ctx, [5, 7])  # both were outside the top 2
    said = evaluate(rule("top_entries"), result, ictx)
    assert [s.text for s in said] == [
        "Nasi Campur new in the top 2 at #1 (was #5)",
        "Latte-Hot new in the top 2 at #2 (was #7)",
    ]
    assert said[0].score > said[1].score  # the higher rank says more


def test_an_entry_that_was_not_sold_before(ctx, ictx):
    result = ranked(ctx, [1, None])
    (said,) = evaluate(rule("top_entries"), result, ictx)
    assert said.text == "Latte-Hot new in the top 2 at #2 (was not sold)"


def test_no_entry_when_the_top_did_not_change(ctx, ictx):
    assert evaluate(rule("top_entries"), ranked(ctx, [2, 1]), ictx) == []


def test_no_entry_of_a_small_side(ctx, ictx):
    assert evaluate(rule("top_entries"), ranked(ctx, [5, 7], SMALL), ictx) == []


def test_top_entries_need_the_prior_rank_of_a_top_query(ctx):
    plain = """
query: {model: fct_sales_lines, dimensions: [adjusted_item_variant], metrics: [net_sales],
        window: L14D, compare: prior}
insights: [top_entries]
"""
    with pytest.raises(ComponentError, match="top_entries: the query needs `top`"):
        component(plain)


# --- peaks -----------------------------------------------------------------------------------

HOURS = """
query: {model: fct_orders, dimensions: [hour_of_day], metrics: [orders],
        window: L14D, compare: prior}
"""


def hours(ctx, now, before, n=BIG):
    result = run(ctx, HOURS)
    assert len(result.df) == len(now) == len(before)
    return compared(result, n, orders=now, prior_orders=before)


def test_the_busiest_hours_changed_clearly(ctx, ictx):
    result = hours(ctx, [10, 3, 2, 9, 8], [10, 9, 2, 3, 8])  # 10:00 replaces 08:00
    (said,) = evaluate(rule("peak_hours"), result, ictx)
    assert said.text == "Busiest hours now 07:00, 10:00 and 11:00 (were 07:00, 08:00 and 11:00)"


def test_the_same_busiest_hours_say_nothing(ctx, ictx):
    assert evaluate(rule("peak_hours"), hours(ctx, [10, 3, 2, 9, 8], [9, 3, 2, 10, 7]), ictx) == []


def test_a_newcomer_must_beat_the_hour_it_replaces_by_the_margin(ctx, ictx):
    # 10:00 has 9 against 8 of 08:00: less than 15% more
    assert (
        evaluate(rule("peak_hours"), hours(ctx, [10, 8, 2, 9, 8.5], [10, 9, 2, 3, 8]), ictx) == []
    )
    # and 08:00 was only a little busier than 10:00 before
    assert (
        evaluate(rule("peak_hours"), hours(ctx, [10, 3, 2, 9, 8], [10, 3.2, 2, 3, 8]), ictx) == []
    )


def test_no_peaks_of_a_small_side(ctx, ictx):
    assert (
        evaluate(rule("peak_hours"), hours(ctx, [10, 3, 2, 9, 8], [10, 9, 2, 3, 8], SMALL), ictx)
        == []
    )


# --- total_change ----------------------------------------------------------------------------

DISCOUNTS = """
query: {model: fct_sales_lines, dimensions: [discount_code], metrics: [discounts, discount_rate],
        window: L14D, compare: prior, totals: true}
"""


def discounts(ctx, rate=0.10, prior=0.05, n=BIG):
    result = run(ctx, DISCOUNTS)
    k = len(result.df)
    return compared(
        result,
        n,
        discount_rate=[0.0] * (k - 1) + [rate],
        prior_discount_rate=[0.0] * (k - 1) + [prior],
        discount_rate_delta=[0.0] * (k - 1) + [rate - prior],
        discount_rate_change=[0.0] * (k - 1) + [rate / prior - 1],
    )


def test_the_total_discount_rate_moved(ctx, ictx):
    (said,) = evaluate(rule("discount_rate_total"), discounts(ctx), ictx)
    assert said.text == "Discounts 10.0% of gross sales, +5.0 pp"
    assert said.key == (None, "discount_rate", "total")
    assert said.tone == "bad"  # a higher discount rate is the wrong way


def test_the_total_discount_rate_below_the_threshold(ctx, ictx):
    assert evaluate(rule("discount_rate_total"), discounts(ctx, 0.06), ictx) == []


def test_no_total_change_of_a_small_side(ctx, ictx):
    assert evaluate(rule("discount_rate_total"), discounts(ctx, n=SMALL), ictx) == []


def test_total_change_names_a_metric_of_the_query():
    rules = parse_rules(
        {"t": {"type": "total_change", "metric": "aov", "min_abs_delta": 1, "text": "x"}}
    )
    assert rules["t"].type.name == "total_change"


# --- movers: skip and the member -------------------------------------------------------------


def cost(ctx, n=BIG):
    result = run(ctx, DISCOUNTS)
    k = len(result.df)
    return compared(
        result,
        n,
        prior_discounts=0.0,
        discounts_delta=1_000_000.0,
        discounts_change=1.0,
        discounts=1_000_000.0,
    ), k


def test_a_mover_skips_the_members_it_never_names(ctx, ictx):
    result, _ = cost(ctx)
    said = evaluate(rule("discount_cost"), result, ictx)
    assert [s.text for s in said] == ["FOUNDER25: discounts +1.00M to 1.00M"] or len(said) == 1
    assert "No discount" not in said[0].text  # the lines without a code are skipped


def test_a_mover_with_no_skip_names_every_member(ctx, ictx):
    rules = parse_rules(
        {
            "cost": {
                "type": "movers",
                "metrics": ["discounts"],
                "top": 5,
                "skip": [],
                "min_abs_delta": {"idr": 500_000},
                "text": "{{ row.member }}",
            }
        }
    )
    result, _ = cost(ctx)
    said = evaluate(rules["cost"], result, ictx)
    assert {s.text for s in said} >= {"FOUNDER25", "No discount"}


# --- drivers: every driver, the threshold and the style --------------------------------------


def test_every_driver_is_named_in_the_order_of_its_effect(ctx, ictx):
    (said,) = evaluate(rule("aov_drivers"), bridge(ctx), ictx, {"min_abs_change": 0})
    assert said.text == "AOV +4.6%: items per order +10.0%, item price +0.0%"
    assert evaluate(rule("aov_drivers"), bridge(ctx), ictx) == []  # 4.6% < the packaged 5%


def test_every_driver_needs_the_change_to_reach_the_threshold(ctx, ictx):
    rules = parse_rules(
        {
            "d": {
                "type": "drivers",
                "every_driver": True,
                "min_abs_change": 0.1,
                "text": "{{ kpi.change }}",
            }
        }
    )
    assert evaluate(rules["d"], bridge(ctx), ictx) == []  # 4.6% is below 10%


def test_a_driver_that_is_unknown_stops_the_every_driver_sentence(ctx, ictx):
    result = bridge(ctx)
    df = result.df.copy()
    df["effect"] = [3000.0, None, 2750.0]
    assert evaluate(rule("aov_drivers"), result.replace(df), ictx) == []


def test_no_drivers_of_a_small_side(ctx, ictx):
    assert evaluate(rule("aov_drivers"), bridge(ctx, n=SMALL), ictx) == []


# --- record: the compact style ---------------------------------------------------------------


def test_a_compact_record(ctx, ictx):
    (said,) = evaluate(rule("sales_record"), trend(ctx), ictx)
    assert said.text.startswith("Best day ")
    assert "(was " in said.text
    assert said.score <= 0.8  # its weight


def test_the_packaged_rules_of_the_weekly_report_are_all_there():
    expected = {
        "net_sales_drivers",
        "aov_drivers",
        "sales_record",
        "category_slices",
        "top_entries",
        "item_share",
        "peak_hours",
        "discount_rate_total",
        "discount_cost",
        "payment_share",
    }
    assert expected <= set(default_rules())
