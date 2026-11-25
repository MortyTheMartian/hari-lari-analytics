"""The transform library (plan 7.10): hand-computed tests on small made-up results and on the
fixture of `test_compiler`, up to Tue 13 Oct 2026:

    R001 Mon 05 Oct 07:00 BCA    Latte (Coffee) 40,000 + Focaccia (Snacks) 28,000
    R002 Mon 05 Oct 08:00 Cash   Latte (Coffee) 40,000 - 10,000 FOUNDER25 discount
    R003 Tue 06 Oct 09:00 Gopay  Cappucino (Coffee) 40,000, fully refunded: not an order
    R004 Mon 12 Oct 10:00 BCA    2 x Nasi Campur (Mains) 35,000 + Mystery Cake (Cakes) 30,000
    R005 Tue 13 Oct 11:00 BCA    Custom Amount 15,000

Trading days: Mon 5, Mon 12 and Tue 13 Oct (6 Oct had no order).
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.reporting import formats
from hari_lari_analytics.reporting.compiler import Query, run_query
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.registry import default_registry
from hari_lari_analytics.reporting.result import Result, ResultMeta
from hari_lari_analytics.reporting.transforms import (
    TRANSFORMS,
    TransformContext,
    TransformError,
    apply,
    parse_step,
    plan_query,
    run_pipeline,
)
from hari_lari_analytics.reporting.transforms.quality import small_n_mask
from hari_lari_analytics.reporting.windows import Calendar

from .api_fixture import api_tables
from .test_compiler import ANCHOR, PERIODS, real, real_run, rq, same  # noqa: F401  (real_run: a fixture)
from .test_models import LAST_SALE, SEEDS

REG = default_registry()


@pytest.fixture(scope="module")
def con():
    return connect(api_tables(), SEEDS, LAST_SALE)


@pytest.fixture(scope="module")
def ctx(con):
    return TransformContext(REG, Calendar.from_db(con))


def q(con, **query) -> Result:
    return run_query(con, Query(**query), REG)


def made(df: dict, metrics: list[str] = (), dimensions: list[str] = (), counts=("orders",),
         shape="rows", **meta) -> Result:  # fmt: skip
    """A result by hand: `df` has the columns of the convention; `metrics` and `counts` give the
    specs of the metric columns and of the `n_<count>` columns."""
    specs = {m: REG.metric_on(m, "fct_sales_lines") for m in metrics}
    specs |= {f"n_{c}": REG.metric_on(c, "fct_sales_lines") for c in counts if f"n_{c}" in df}
    frame = pd.DataFrame(df)
    if "is_total" not in frame:
        frame["is_total"] = False
    return Result(
        frame,
        ResultMeta(
            shape=shape,
            window=None,
            empty=False,
            metrics=specs,
            dimensions={d: REG.dimension(d) for d in dimensions},
            **meta,
        ),  # fmt: skip
    )


def col(result: Result, column: str) -> list:
    return [None if pd.isna(v) else v for v in result.df[column]]


def run(name, result, ctx, **options):
    return apply(name, options, result, ctx)


# --- the registry -------------------------------------------------------------------------------


def test_every_transform_of_the_plan_is_registered():
    planned = {"sort_limit", "rank", "fill_domain", "per_trading_day", "growth_vs_previous",
               "cumulative_share", "pivot", "classify", "suppress_small_n"}  # fmt: skip
    assert planned <= set(TRANSFORMS)
    assert all(t.description for t in TRANSFORMS.values())  # each has a docstring for the gallery


def test_steps_are_a_name_or_a_name_with_options():
    assert parse_step("per_trading_day") == ("per_trading_day", {})
    assert parse_step({"pivot": {"rows": "weekday"}}) == ("pivot", {"rows": "weekday"})
    assert parse_step({"fill_domain": None}) == ("fill_domain", {})
    for bad in ({"a": {}, "b": {}}, 5, {"pivot": "weekday"}):
        with pytest.raises(TransformError, match="is not a transform"):
            parse_step(bad)


def test_unknown_transforms_options_and_shapes_fail_with_a_message(ctx):
    r = made({"adjusted_category": ["a"], "net_sales": [1.0]}, ["net_sales"], ["adjusted_category"])
    with pytest.raises(
        TransformError, match=r"unknown transform 'sortlimit' \(known: .*sort_limit"
    ):
        apply("sortlimit", {}, r, ctx)
    with pytest.raises(TransformError, match="sort_limit: n: Input should be greater"):
        run("sort_limit", r, ctx, by="net_sales", n=0)
    with pytest.raises(TransformError, match="sort_limit: nn: Extra inputs are not permitted"):
        run("sort_limit", r, ctx, by="net_sales", nn=3)
    with pytest.raises(TransformError, match="sort_limit: by: Field required"):
        run("sort_limit", r, ctx)
    with pytest.raises(TransformError, match="no column nope"):
        run("sort_limit", r, ctx, by="nope")
    series = Result(r.df, ResultMeta(shape="series", window=None, empty=False))
    with pytest.raises(TransformError, match="does not work on a result of shape series"):
        run("sort_limit", series, ctx, by="net_sales")


def test_a_transform_returns_a_new_result_and_leaves_the_input_alone(ctx):
    r = made({"adjusted_category": ["a", "b"], "net_sales": [1.0, 2.0]}, ["net_sales"],
             ["adjusted_category"])  # fmt: skip
    before = r.df.copy()
    out = run("rank", r, ctx, by="net_sales")
    assert out is not r and "rank" in out.df and "rank" not in r.df
    pd.testing.assert_frame_equal(r.df, before)


# --- sort_limit and rank ------------------------------------------------------------------------


def category_result(**extra) -> Result:
    """Rows a to d, an Other row and a total row."""
    names = ["a", "b", "c", "d", "Other (3)", None]
    return made(
        {
            "adjusted_category": names,
            "is_total": [False] * 5 + [True],
            "is_other": [False] * 4 + [True, False],
            "net_sales": [5, 9, np.nan, 9, 100, 123],
            "orders": [1, 2, 3, 4, 5, 6],
            **extra,
        },
        ["net_sales", "orders"],
        ["adjusted_category"],
    )


def test_sort_limit_sorts_stably_and_keeps_other_and_total_at_the_end(ctx):
    r = run("sort_limit", category_result(), ctx, by="net_sales")
    # b and d tie at 9 and keep their order; NULL last among the ranked rows
    assert col(r, "adjusted_category") == ["b", "d", "a", "c", "Other (3)", None]
    r = run("sort_limit", category_result(), ctx, by="net_sales", desc=False, n=2)
    assert col(r, "adjusted_category") == ["a", "b", "Other (3)", None]
    r = run("sort_limit", category_result(), ctx, by="orders", n=1)
    assert col(r, "adjusted_category") == ["d", "Other (3)", None]
    assert list(r.df.index) == [0, 1, 2]


def test_rank_with_ties_and_without_a_rank_for_other_and_total(ctx):
    r = run("rank", category_result(), ctx, by="net_sales")  # min: 1, 1, 3 for the tie
    assert col(r, "rank") == [3, 1, None, 1, None, None]
    assert col(run("rank", category_result(), ctx, by="net_sales", method="dense"), "rank")[:4] == [
        2, 1, None, 1,
    ]  # fmt: skip
    assert col(run("rank", category_result(), ctx, by="net_sales", method="first"), "rank")[:4] == [
        3, 1, None, 2,
    ]  # fmt: skip
    r = run("rank", category_result(), ctx, by="net_sales", desc=False, into="low")
    assert col(r, "low")[:4] == [1, 2, None, 2]
    assert str(r.df["low"].dtype) == "Int64"
    with pytest.raises(TransformError, match="the column rank already exists"):
        run("rank", r.replace(r.df.assign(rank=1)), ctx, by="net_sales")


def test_rank_within_the_time_bucket(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket", "payment_type"], window="L2W",
          grain="week", metrics=["net_sales"], where={"payment_type": ["BCA", "Cash"]})  # fmt: skip
    out = run("rank", r, ctx, by="net_sales", within=["bucket"])
    # the week of 28 Sep has no sales (zeros tie at 1); the week of 5 Oct: BCA 68,000, Cash 30,000
    week2 = out.df[out.df["bucket"] == pd.Timestamp("2026-10-05")]
    assert dict(zip(week2["payment_type"], week2["rank"], strict=True)) == {"BCA": 1, "Cash": 2}
    week1 = out.df[out.df["bucket"] == pd.Timestamp("2026-09-28")]
    assert set(week1["rank"]) == {1}


# --- fill_domain --------------------------------------------------------------------------------


def test_fill_domain_adds_every_weekday_with_zeros_and_its_trading_days(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday"], window="full", totals=True,
          metrics=["net_sales", "orders", "aov"])  # fmt: skip
    out = run("fill_domain", r, ctx)
    assert col(out, "weekday") == [1, 2, 3, 4, 5, 6, 7, None]
    assert col(out, "orders") == [3, 1, 0, 0, 0, 0, 0, 4]
    assert col(out, "net_sales")[:2] == [198_000, 15_000]
    assert col(out, "net_sales")[2:7] == [0] * 5
    assert math.isnan(out.df["aov"][2]) and out.df["aov"][0] == 66_000  # a ratio has no value
    # Mon 5 and 12 Oct, Tue 13 Oct; nobody sold on the other weekdays
    assert col(out, "trading_days") == [2, 1, 0, 0, 0, 0, 0, 3]
    assert col(out, "n_orders")[:3] == [3, 1, 0]
    assert out.df["is_total"].tolist() == [False] * 7 + [True]  # the total row stays last
    assert col(out, "weekday")[:2] == col(r, "weekday")[:2]  # existing rows unchanged
    assert r.df.shape == (3, out.df.shape[1])  # the input is untouched


def test_fill_domain_gives_a_missing_weekday_the_trading_days_of_the_calendar(con, ctx):
    # an extra (hand-made) Wednesday trading day does not exist in the fixture, so use a
    # calendar that has one: the weekday is missing from the result but 3 Wednesdays traded
    wednesdays = {date(2026, 10, 7), date(2026, 10, 14), date(2026, 10, 21)}
    calendar = Calendar(date(2026, 10, 25), date(2026, 10, 5),
                        frozenset({date(2026, 10, 5), *wednesdays}))  # fmt: skip
    r = q(con, model="fct_orders", dimensions=["weekday"], window="full", metrics=["orders"])
    out = run("fill_domain", r, TransformContext(REG, calendar))
    # the window of the result is 5 to 13 Oct, so only 7 Oct counts
    assert dict(zip(col(out, "weekday"), col(out, "trading_days"), strict=True))[3] == 1
    without_calendar = run("fill_domain", r, TransformContext(REG, None))
    assert math.isnan(without_calendar.df.loc[2, "trading_days"])


def test_fill_domain_adds_the_missing_bins_and_the_hours_between_the_first_and_last(con, ctx):
    r = q(con, model="fct_orders", dimensions=["basket_size"], window="full", metrics=["orders"])
    assert col(r, "basket_size") == ["1", "2", "3", None]  # bins 4 and 5+ never sold
    out = run("fill_domain", r, ctx)
    assert col(out, "basket_size") == ["1", "2", "3", "4", "5+", None]
    assert col(out, "orders") == [2, 1, 1, 0, 0, 0]
    # with bins from the query, the result's dimension knows them: no 4 and 5+ here
    r = q(con, model="fct_orders", dimensions=["basket_size"], window="full", metrics=["orders"],
          bins={"basket_size": [1, "2+"]})  # fmt: skip
    assert col(run("fill_domain", r, ctx), "basket_size") == ["1", "2+", None]

    hours = q(con, model="fct_orders", dimensions=["hour_of_day"], window="full",
              metrics=["orders"], where={"payment_type": "BCA"})  # fmt: skip
    assert col(hours, "hour_of_day") == [7, 10, 11]
    out = run("fill_domain", hours, ctx)
    assert col(out, "hour_of_day") == [7, 8, 9, 10, 11]
    assert col(out, "orders") == [1, 0, 0, 1, 1]
    assert col(out, "trading_days") == [3] * 5  # an hour has the trading days of the window


def test_fill_domain_on_two_dimensions_fills_the_cross_product_in_order(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
          metrics=["orders"])  # fmt: skip
    assert len(r.df) == 5
    out = run("fill_domain", r, ctx)
    assert len(out.df) == 7 * 5  # the 7 weekdays by the hours 7 to 11
    assert out.df[["weekday", "hour_of_day"]].values.tolist() == [
        [w, h] for w in range(1, 8) for h in range(7, 12)
    ]  # fmt: skip
    cells = {(w, h): (n, td) for w, h, n, td in zip(
        out.df["weekday"], out.df["hour_of_day"], out.df["orders"], out.df["trading_days"],
        strict=True)}  # fmt: skip
    assert cells[(1, 7)] == (1, 2) and cells[(2, 11)] == (1, 1)  # rows from the query
    assert cells[(1, 9)] == (0, 2)  # a filled hour of a Monday: the Monday trading days
    assert cells[(3, 7)] == (0, 0)  # a Wednesday: no trading days


def test_fill_domain_on_one_dimension_fills_inside_each_group_of_the_others(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
          metrics=["orders"])  # fmt: skip
    out = run("fill_domain", r, ctx, dimensions=["hour_of_day"])
    assert len(out.df) == 2 * 5  # Monday and Tuesday only, as in the query
    assert out.df["weekday"].tolist() == [1] * 5 + [2] * 5
    assert out.df["hour_of_day"].tolist() == [7, 8, 9, 10, 11] * 2


def test_fill_domain_with_given_values_and_its_errors(con, ctx):
    r = q(con, model="fct_orders", dimensions=["payment_type"], window="full",
          metrics=["orders"], totals=True)  # fmt: skip
    with pytest.raises(TransformError, match="payment_type has no domain to fill"):
        run("fill_domain", r, ctx, dimensions=["payment_type"])
    assert run("fill_domain", r, ctx).df.shape == r.df.shape  # nothing fillable: unchanged
    out = run("fill_domain", r, ctx, values={"payment_type": ["BCA", "Cash", "Gopay", "Visa"]})
    assert col(out, "payment_type") == ["BCA", "Cash", "Gopay", "Visa", None]
    assert col(out, "orders") == [3, 1, 0, 0, 4]
    with pytest.raises(TransformError, match="has_food is not a dimension"):
        run("fill_domain", r, ctx, dimensions=["has_food"])
    series = q(con, model="fct_orders", dimensions=["bucket"], window="WTD", metrics=["orders"])
    with pytest.raises(TransformError, match="does not work on a result of shape series"):
        run("fill_domain", series, ctx)
    empty = q(con, model="fct_orders", dimensions=["weekday"], window="L3M", metrics=["orders"])
    assert run("fill_domain", empty, ctx) is empty  # no sales: no empty table of zeros


def test_a_filled_row_has_no_comparison(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday"], window="WTD", compare="prior",
          metrics=["net_sales", "orders"])  # fmt: skip
    out = run("fill_domain", r, ctx)
    wed = out.df[out.df["weekday"] == 3].iloc[0]
    assert (wed.net_sales, wed.orders, wed.n_orders) == (0, 0, 0)
    unknown = ["prior_net_sales", "net_sales_change", "net_sales_delta", "prior_n_orders"]
    assert all(pd.isna(wed[c]) for c in unknown)
    # WTD is Mon 12 and Tue 13 Oct; the week before had Mon 5 (and a closed Tue 6): the calendar
    assert (wed.trading_days, wed.prior_trading_days) == (0, 0)


# --- per_trading_day ----------------------------------------------------------------------------


def test_per_trading_day_by_weekday_and_hour_uses_the_trading_days_of_each_weekday(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
          metrics=["orders"], totals=True)  # fmt: skip
    out = run("per_trading_day", r, ctx)
    # Mondays (2 trading days): 7h, 8h and 10h one order each; Tuesday (1): 9h none, 11h one
    assert col(out, "orders") == [0.5, 0.5, 0.5, 0, 1.0, 4 / 3]
    assert col(out, "n_orders") == [1, 1, 1, 0, 1, 4]  # the sample size stays a count
    assert out.meta.metrics["orders"].format == "decimal"
    assert out.meta.metrics["orders"].label == "Orders per trading day"
    assert r.meta.metrics["orders"].format == "count"  # the input's metadata is untouched


def test_per_trading_day_of_one_row_recomputes_the_changes(con, ctx):
    r = q(con, model="fct_orders", dimensions=[], window="WTD", compare="prior",
          metrics=["net_sales", "orders", "aov"])  # fmt: skip
    out = run("per_trading_day", r, ctx)
    row = out.df.iloc[0]
    # this week 2 trading days, the week before 1
    assert (row.net_sales, row.prior_net_sales) == (57_500, 98_000)
    assert row.net_sales_change == pytest.approx(57_500 / 98_000 - 1)
    assert row.net_sales_delta == -40_500
    assert (row.orders, row.prior_orders) == (1.0, 2.0)
    assert row.orders_change == pytest.approx(-0.5)
    assert row.aov == 57_500 and row.prior_aov == 49_000  # a ratio stays as it is
    assert out.meta.metrics["aov"] is r.meta.metrics["aov"]
    assert (row.n_orders, row.prior_n_orders) == (2, 2)


def test_per_trading_day_in_a_series_divides_each_bucket_by_its_own_days(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="L14D", grain="block_7d",
          metrics=["net_sales"], totals=True)  # fmt: skip
    out = run("per_trading_day", r, ctx)
    # blocks 30 Sep to 6 Oct (trading day Mon 5) and 7 to 13 Oct (Mon 12 and Tue 13): 98,000 / 1
    # and 115,000 / 2; the window total 213,000 / 3
    assert col(out, "net_sales") == [98_000, 57_500, 71_000]
    assert col(out, "trading_days") == [1, 2, 3]


def test_per_trading_day_gives_no_value_without_trading_days(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="WTD", metrics=["orders"],
          buckets_before=1)  # fmt: skip
    out = run("per_trading_day", r, ctx)  # Sun 11 Oct has no sales and no trading day
    assert col(out, "orders") == [None, 1.0, 1.0]


def test_per_trading_day_refuses_ratios_and_a_second_division(con, ctx):
    r = q(con, model="fct_orders", dimensions=["payment_type"], window="full",
          metrics=["net_sales", "aov", "share"])  # fmt: skip
    out = run("per_trading_day", r, ctx)  # default: only the metrics that sum
    assert col(out, "net_sales")[0] == 183_000 / 3
    assert col(out, "aov") == col(r, "aov") and col(out, "share") == col(r, "share")
    with pytest.raises(TransformError, match="aov does not sum over trading days"):
        run("per_trading_day", r, ctx, metrics=["aov"])
    with pytest.raises(TransformError, match="share does not sum"):
        run("per_trading_day", r, ctx, metrics=["share"])  # a relative metric
    with pytest.raises(TransformError, match="net_sales does not sum"):
        run("per_trading_day", out, ctx, metrics=["net_sales"])
    with pytest.raises(TransformError, match="gross_sales is not a metric of the result"):
        run("per_trading_day", r, ctx, metrics=["gross_sales"])
    only = run("per_trading_day", r, ctx, metrics=["net_sales"])
    assert only.meta.metrics["net_sales"].additivity == "none"


# --- growth_vs_previous -------------------------------------------------------------------------


def weekly(con, **extra) -> Result:
    """The week of 12 Oct (net sales 115,000, 2 orders) and the week before (98,000, 2 orders)."""
    return q(con, model="fct_orders", window="WTD", grain="week", buckets_before=1,
             metrics=["net_sales", "orders"], **extra)  # fmt: skip


def test_growth_of_the_first_bucket_uses_the_bucket_before_the_window(con, ctx):
    r = weekly(con, dimensions=["bucket"])
    assert r.df["in_window"].tolist() == [False, True]
    out = run("growth_vs_previous", r, ctx)
    assert "in_window" not in out.df and len(out.df) == 1
    assert out.df["net_sales_growth"].iloc[0] == pytest.approx(115_000 / 98_000 - 1)
    assert out.df["orders_growth"].iloc[0] == 0
    assert [b.ordinal for b in out.meta.buckets] == [0]
    kept = run("growth_vs_previous", r, ctx, keep_before=True)
    assert len(kept.df) == 2 and pd.isna(kept.df["net_sales_growth"].iloc[0])  # nothing before it
    assert [b.ordinal for b in kept.meta.buckets] == [-1, 0]


def test_growth_per_group_with_bucket_totals_and_no_growth_for_the_window_total(con, ctx):
    r = weekly(con, dimensions=["bucket", "payment_type"], totals=True)
    assert col(r, "payment_type") == ["BCA", None, "BCA", None, None]
    assert col(r, "is_total") == [False, True, False, True, True]
    assert [pd.isna(b) for b in r.df["bucket"]] == [False, False, False, False, True]
    assert r.df["in_window"].tolist() == [False, False, True, True, True]
    out = run("growth_vs_previous", r, ctx, metrics=["net_sales"])
    rows = out.df
    assert len(rows) == 3 and "orders_growth" not in rows
    # BCA: 115,000 against 68,000; the bucket total: 115,000 against 98,000; the window total: none
    assert col(out, "net_sales_growth") == [
        pytest.approx(115_000 / 68_000 - 1), pytest.approx(115_000 / 98_000 - 1), None,
    ]  # fmt: skip


def test_the_growth_of_a_rate_is_the_difference_in_percentage_points(con, ctx):
    r = q(con, model="fct_orders", window="WTD", grain="week", buckets_before=1,
          dimensions=["bucket"], metrics=["discount_rate"])  # fmt: skip
    before, now = r.df["discount_rate"].tolist()
    assert before > 0 and now == 0  # FOUNDER25 in the week of 5 Oct, no discount after
    out = run("growth_vs_previous", r, ctx)
    assert out.df["discount_rate_growth"].iloc[0] == pytest.approx(now - before)
    spec = out.meta.metrics["discount_rate"]
    text = formats.comparison_text(now - before, spec, "growth", "compact")
    assert text.endswith("pp") and text.startswith("-")


def test_a_previous_value_of_zero_gives_no_growth(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="WTD", metrics=["net_sales"],
          buckets_before=1)  # fmt: skip
    # daily: Sun 11 Oct (before the window) sold nothing, Mon 12 100,000, Tue 13 15,000
    out = run("growth_vs_previous", r, ctx, grains=["day"])
    assert col(out, "net_sales_growth") == [None, pytest.approx(15_000 / 100_000 - 1)]


def test_the_daily_view_gets_no_growth_by_default(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="WTD", metrics=["net_sales"],
          buckets_before=1)  # fmt: skip
    out = run("growth_vs_previous", r, ctx)
    assert col(out, "net_sales_growth") == [None, None] and len(out.df) == 2


def test_the_first_bucket_has_no_growth_without_a_bucket_before(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="WTD", grain="week",
          metrics=["net_sales"])  # fmt: skip
    assert col(run("growth_vs_previous", r, ctx), "net_sales_growth") == [None]


def test_growth_needs_a_series_and_known_metrics(con, ctx):
    rows = q(con, model="fct_orders", dimensions=["payment_type"], window="full",
             metrics=["net_sales"])  # fmt: skip
    with pytest.raises(TransformError, match="does not work on a result of shape rows"):
        run("growth_vs_previous", rows, ctx)
    with pytest.raises(TransformError, match="gross_sales is not a metric of the result"):
        run("growth_vs_previous", weekly(con, dimensions=["bucket"]), ctx, metrics=["gross_sales"])


# --- cumulative_share ---------------------------------------------------------------------------


def test_cumulative_share_sorts_largest_first_and_runs_over_the_total(ctx):
    r = made(
        {"adjusted_category": ["a", "b", "c", "d", None], "net_sales": [10, 50, 30, 10, 100],
         "is_total": [False] * 4 + [True]},
        ["net_sales"], ["adjusted_category"],
    )  # fmt: skip
    out = run("cumulative_share", r, ctx, metric="net_sales")
    assert col(out, "adjusted_category") == ["b", "c", "a", "d", None]
    assert col(out, "cumulative_share") == [0.5, 0.8, 0.9, 1.0, None]
    unsorted = run("cumulative_share", r, ctx, metric="net_sales", sort=False, into="run")
    assert col(unsorted, "run")[:4] == [0.1, 0.6, 0.9, 1.0]


def test_cumulative_share_of_a_top_n_reads_against_all_sales_and_other_comes_last(con, ctx):
    top = dict(model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
               metrics=["net_sales"], totals=True)  # fmt: skip
    r = q(con, top={"n": 2, "by": "net_sales"}, **top)  # Coffee and Mains, 70,000 each
    out = run("cumulative_share", r, ctx, metric="net_sales")
    assert col(out, "cumulative_share") == [
        pytest.approx(70_000 / 213_000), pytest.approx(140_000 / 213_000), None,
    ]  # fmt: skip
    r = q(con, top={"n": 2, "by": "net_sales", "other": True}, **top)
    out = run("cumulative_share", r, ctx, metric="net_sales")
    assert col(out, "adjusted_category")[-2:] == ["Other (3)", None]
    assert col(out, "cumulative_share")[-2:] == [1.0, None]  # Other brings it to the total


def test_cumulative_share_without_a_total_row_uses_the_rows(con, ctx):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
          metrics=["items_sold"])  # fmt: skip
    out = run("cumulative_share", r, ctx, metric="items_sold")
    assert col(out, "cumulative_share")[-1] == 1.0
    assert out.df["cumulative_share"].is_monotonic_increasing


def test_cumulative_share_only_accepts_metrics_that_add_up(con, ctx):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
          metrics=["net_sales", "aov", "orders"])  # fmt: skip
    with pytest.raises(TransformError, match="aov is not additive"):
        run("cumulative_share", r, ctx, metric="aov")
    with pytest.raises(TransformError, match="receipt can fall into several groups"):
        run("cumulative_share", r, ctx, metric="orders")  # a receipt has several categories
    with pytest.raises(TransformError, match="gross_sales is not a metric"):
        run("cumulative_share", r, ctx, metric="gross_sales")
    by_payment = q(con, model="fct_orders", dimensions=["payment_type"], window="full",
                   metrics=["orders"])  # fmt: skip
    out = run("cumulative_share", by_payment, ctx, metric="orders")  # one payment per receipt
    assert col(out, "payment_type") == ["BCA", "Cash", "Gopay"]  # 3, 1 and 0 of the 4 orders
    assert col(out, "cumulative_share") == [0.75, 1.0, 1.0]


# --- pivot --------------------------------------------------------------------------------------


def test_pivot_weekday_by_hour_of_orders_per_trading_day(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
          metrics=["orders"], totals=True)  # fmt: skip
    out = run_pipeline(
        r,
        [
            "fill_domain",
            "per_trading_day",
            {"pivot": {"rows": "weekday", "columns": "hour_of_day"}},
        ],
        ctx,
    )
    assert out.meta.shape == "matrix"
    assert (out.meta.matrix.rows, out.meta.matrix.columns, out.meta.matrix.value) == (
        "weekday", "hour_of_day", "orders")  # fmt: skip
    assert out.meta.matrix.column_values == (7, 8, 9, 10, 11)
    m = out.matrix_frame()
    assert m.index.tolist() == [1, 2, 3, 4, 5, 6, 7] and m.columns.tolist() == [7, 8, 9, 10, 11]
    assert m.loc[1].tolist() == [0.5, 0.5, 0.0, 0.5, 0.0]
    assert m.loc[2].tolist() == [0.0, 0.0, 0.0, 0.0, 1.0]
    assert np.isnan(m.loc[3]).all()  # Wednesday: 0 orders on 0 trading days
    assert out.meta.dimensions["weekday"].labels[1] == "Mon"  # the labels travel with the result


def test_pivot_leaves_empty_cells_empty_and_names_null_values(con, ctx):
    r = q(con, model="fct_orders", dimensions=["payment_type", "has_food"], window="full",
          metrics=["net_sales"])  # fmt: skip
    out = run("pivot", r, ctx, rows="payment_type", columns="has_food")
    # BCA: R004 has food (100,000), R001 and R005 do not (68,000 + 15,000); Cash 30,000 without
    # food; the refunded Gopay receipt sold nothing. A combination that never occurs is empty.
    assert out.df.columns.tolist() == ["payment_type", True, False]
    cells = out.matrix_frame()
    assert cells.loc["BCA"].tolist() == [100_000, 83_000]
    assert np.isnan(cells.loc["Cash", True]) and cells.loc["Cash", False] == 30_000
    assert np.isnan(cells.loc["Gopay", True]) and cells.loc["Gopay", False] == 0
    nulls = q(con, model="fct_sales_lines", dimensions=["adjusted_category", "discount_code"],
              window="full", metrics=["net_sales"])  # fmt: skip
    got = run("pivot", nulls, ctx, rows="adjusted_category", columns="discount_code")
    assert "No discount" in got.df.columns and "FOUNDER25" in got.df.columns


def test_pivot_errors(con, ctx):
    r = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
          metrics=["orders", "net_sales"])  # fmt: skip
    with pytest.raises(TransformError, match="name the `value` column, the result has 2 metrics"):
        run("pivot", r, ctx, rows="weekday", columns="hour_of_day")
    out = run("pivot", r, ctx, rows="weekday", columns="hour_of_day", value="net_sales")
    assert out.meta.matrix.value == "net_sales"
    with pytest.raises(TransformError, match="no column nope"):
        run("pivot", r, ctx, rows="weekday", columns="hour_of_day", value="nope")
    with pytest.raises(TransformError, match="rows and columns must be different"):
        run("pivot", r, ctx, rows="weekday", columns="weekday", value="orders")
    with pytest.raises(TransformError, match="payment_type is not a dimension of the result"):
        run("pivot", r, ctx, rows="weekday", columns="payment_type", value="orders")
    three = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day", "payment_type"],
              window="full", metrics=["orders"])  # fmt: skip
    with pytest.raises(TransformError, match="also varies by payment_type"):
        run("pivot", three, ctx, rows="weekday", columns="hour_of_day")
    text = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
             metrics=["first_sale_ts"])  # fmt: skip
    with pytest.raises(TransformError, match="the column first_sale_ts is not numeric"):
        run("pivot", text, ctx, rows="weekday", columns="hour_of_day")
    with pytest.raises(ValueError, match="not a matrix"):
        r.matrix_frame()


def test_pivot_of_a_series_puts_the_buckets_in_the_rows(con, ctx):
    r = q(con, model="fct_orders", dimensions=["bucket", "payment_type"], window="WTD",
          metrics=["net_sales"], where={"payment_type": ["BCA", "Cash"]})  # fmt: skip
    out = run("pivot", r, ctx, rows="bucket", columns="payment_type")
    m = out.matrix_frame()
    assert m.index.tolist() == [pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-13")]
    assert m.columns.tolist() == ["BCA"]  # Cash sold nothing this week, so it is no group
    assert m["BCA"].tolist() == [100_000, 15_000]


# --- classify -----------------------------------------------------------------------------------


def test_classify_thresholds_make_abc_classes_from_the_cumulative_share(ctx):
    r = made(
        {"adjusted_item_variant": ["a", "b", "c", "d", "Other (2)", None],
         "net_sales": [50, 30, 10, 5, 5, 100],
         "is_total": [False] * 5 + [True], "is_other": [False] * 4 + [True, False]},
        ["net_sales"], ["adjusted_item_variant"],
    )  # fmt: skip
    out = run_pipeline(
        r,
        [
            {"cumulative_share": {"metric": "net_sales"}},
            {
                "classify": {
                    "mode": "thresholds",
                    "by": "cumulative_share",
                    "into": "abc",
                    "bands": [{"upto": 0.8, "label": "A"}, {"upto": 0.95, "label": "B"}],
                    "otherwise": "C",
                }
            },
        ],  # fmt: skip
        ctx,
    )
    # shares 0.5, 0.8, 0.9, 0.95, 1.0: the bound is inclusive; Other and the total get no class
    assert col(out, "abc") == ["A", "A", "B", "B", None, None]
    info = out.meta.classifications["abc"]
    assert (info.mode, info.classes, info.splits) == ("thresholds", ("A", "B", "C"),
                                                      {"A": 0.8, "B": 0.95})  # fmt: skip
    assert col(run("classify", r, ctx, mode="thresholds", by="net_sales",
                   bands=[{"upto": 10, "label": "low"}], otherwise="high"), "class") == [
        "high", "high", "low", "low", None, None]  # fmt: skip


def test_classify_quadrants_split_at_the_medians_and_keep_them(ctx):
    r = made(
        {"adjusted_item_variant": ["star", "horse", "puzzle", "dog", "gap", None],
         "items_sold": [90, 80, 20, 10, 50, 250], "gross_sales": [9, 2, 8, 1, np.nan, 20],
         "is_total": [False] * 5 + [True]},
        ["items_sold", "gross_sales"], ["adjusted_item_variant"],
    )  # fmt: skip
    out = run("classify", r, ctx, mode="quadrants", x="items_sold", y="gross_sales",
              labels={"high_high": "Star", "high_low": "Plowhorse", "low_high": "Puzzle",
                      "low_low": "Dog"})  # fmt: skip
    # medians over the four rows with both values: x of 90, 80, 20, 10 is 50; y of 9, 2, 8, 1 is 5
    assert col(out, "class") == ["Star", "Plowhorse", "Puzzle", "Dog", None, None]
    info = out.meta.classifications["class"]
    assert info.splits == {"x": 50.0, "y": 5.0}
    assert info.classes == ("Star", "Plowhorse", "Puzzle", "Dog")
    given = run("classify", r, ctx, mode="quadrants", x="items_sold", y="gross_sales",
                x_split=80, y_split="mean")  # fmt: skip
    # a value on the split is high; the mean of y is 5; classes keep the key names
    assert col(given, "class") == ["high_high", "high_low", "low_high", "low_low", None, None]
    assert given.meta.classifications["class"].splits == {"x": 80.0, "y": 5.0}


def lifecycle_result(**extra) -> Result:
    names = ["new", "rising", "steady", "fading", "gone", "small", "never", "unknown", "Other (1)",
             None]  # fmt: skip
    return made(
        {"adjusted_item_variant": names,
         "items_sold": [10, 150, 100, 60, 0, 6, 0, 5, 9, 0],
         "prior_items_sold": [0, 100, 90, 100, 100, 4, 0, np.nan, 9, 0],
         "n_orders": [10, 150, 100, 60, 0, 6, 0, 5, 9, 0],
         "prior_n_orders": [0, 100, 90, 100, 100, 4, 0, np.nan, 9, 0],
         "is_total": [False] * 9 + [True], "is_other": [False] * 8 + [True, False], **extra},
        ["items_sold"], ["adjusted_item_variant"],
    )  # fmt: skip


def test_classify_the_item_lifecycle(ctx):
    r = lifecycle_result()
    out = run("classify", r, ctx, mode="lifecycle", by="items_sold")
    # new: nothing sold before, however little; +50% rising; +11% steady; -40% fading; sold
    # nothing now (-100%) fading; 6 against 4 is under the sample-size rule; nothing in both
    # periods and an unknown prior get no class; Other and the total get none
    assert col(out, "class") == ["new", "rising", "steady", "fading", "fading", None, None,
                                 None, None, None]  # fmt: skip
    info = out.meta.classifications["class"]
    assert info.classes == ("new", "rising", "steady", "fading")
    assert info.splits == {"rising_at": 0.25, "fading_at": -0.25}


def test_the_lifecycle_thresholds_and_the_sample_size_rule_are_options(ctx):
    r = lifecycle_result()
    loose = run("classify", r, ctx, mode="lifecycle", by="items_sold", rising_at=0.05,
                fading_at=-0.5, min_n=None, into="life")  # fmt: skip
    assert col(loose, "life")[:6] == ["new", "rising", "rising", "steady", "fading", "rising"]
    with pytest.raises(TransformError, match="fading_at must be below rising_at"):
        run("classify", r, ctx, mode="lifecycle", by="items_sold", rising_at=0.1, fading_at=0.2)


def test_the_lifecycle_sample_size_is_the_orders_with_the_item_in_each_period(ctx):
    # 20 orders with the item now and 14 before (+43%): enough at the default of 15 (either
    # period), too few at 30; 12 and 9 are under 15 in both periods
    r = made(
        {"adjusted_item_variant": ["a", "b"], "items_sold": [25, 14], "prior_items_sold": [17, 9],
         "n_orders": [20, 12], "prior_n_orders": [14, 9], "is_total": [False, False]},
        ["items_sold"], ["adjusted_item_variant"],
    )  # fmt: skip
    assert col(run("classify", r, ctx, mode="lifecycle", by="items_sold"), "class") == [
        "rising", None]  # fmt: skip
    assert col(run("classify", r, ctx, mode="lifecycle", by="items_sold", min_n=30), "class") == [
        None, None]  # fmt: skip
    assert col(run("classify", r, ctx, mode="lifecycle", by="items_sold", min_n=9), "class") == [
        "rising", "rising"]  # fmt: skip
    with pytest.raises(TransformError, match="min_n: Input should be greater"):
        run("classify", r, ctx, mode="lifecycle", by="items_sold", min_n=0)


def test_classify_option_errors(ctx):
    r = lifecycle_result()
    with pytest.raises(TransformError, match="mode thresholds needs bands, by, otherwise"):
        run("classify", r, ctx, mode="thresholds")
    with pytest.raises(TransformError, match="mode lifecycle needs by"):
        run("classify", r, ctx, mode="lifecycle")
    with pytest.raises(TransformError, match="x, y do not belong to mode lifecycle"):
        run("classify", r, ctx, mode="lifecycle", by="items_sold", x="a", y="b")
    with pytest.raises(TransformError, match="ascending order"):
        run("classify", r, ctx, mode="thresholds", by="items_sold", otherwise="z",
            bands=[{"upto": 2, "label": "a"}, {"upto": 1, "label": "b"}])  # fmt: skip
    with pytest.raises(TransformError, match="Input should be"):
        run("classify", r, ctx, mode="quadrants", x="a", y="b", labels={"top": "x"})
    with pytest.raises(TransformError, match="no column gross_sales, prior_gross_sales"):
        run("classify", r, ctx, mode="lifecycle", by="gross_sales")
    with pytest.raises(TransformError, match="the column class already exists"):
        run("classify", run("classify", r, ctx, mode="lifecycle", by="items_sold"), ctx,
            mode="lifecycle", by="items_sold")  # fmt: skip


def test_classifying_a_real_result(con, ctx):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="WTD",
          compare="prior", metrics=["items_sold"], keep_prior_only=True)  # fmt: skip
    out = run("classify", r, ctx, mode="lifecycle", by="items_sold", min_n=None)
    got = dict(zip(col(out, "adjusted_category"), col(out, "class"), strict=True))
    # this week: Mains (2 items) and Cakes (1) and Custom Amount (1) are new; Coffee and Snacks
    # sold only the week before (fading to nothing)
    assert got == {"Mains": "new", "Cakes": "new", "Custom Amount": "new", "Coffee": "fading",
                   "Snacks (Focaccia)": "fading"}  # fmt: skip


# --- suppress_small_n ---------------------------------------------------------------------------


def test_small_n_mask_follows_the_counts_named_by_the_metric(ctx):
    r = made(
        {"payment_type": ["a", "b"], "net_sales": [1.0, 2.0], "food_attach_rate": [0.5, 0.5],
         "n_orders": [100, 20], "n_drink_orders": [10, 60]},
        ["net_sales", "food_attach_rate"], ["payment_type"], counts=("orders", "drink_orders"),
    )  # fmt: skip
    assert small_n_mask(r, "net_sales").tolist() == [False, True]  # the default rule: 30 orders
    assert small_n_mask(r, "food_attach_rate").tolist() == [True, False]  # 30 orders with a drink
    with pytest.raises(TransformError, match="the sample-size column prior_n_orders"):
        small_n_mask(r, "net_sales", prior=True)
    with pytest.raises(TransformError, match="nope is not a metric"):
        small_n_mask(r, "nope")


def test_suppress_small_n_blanks_each_value_by_its_own_sample(ctx):
    r = made(
        {"payment_type": ["big", "small now", "small before", "tiny"],
         "net_sales": [100.0, 20.0, 80.0, 5.0], "prior_net_sales": [90.0, 70.0, 10.0, 4.0],
         "net_sales_change": [0.1, -0.7, 7.0, 0.25], "net_sales_delta": [10.0, -50.0, 70.0, 1.0],
         "n_orders": [100, 20, 90, 5], "prior_n_orders": [90, 80, 15, 4]},
        ["net_sales"], ["payment_type"],
    )  # fmt: skip
    out = run("suppress_small_n", r, ctx)
    assert col(out, "net_sales") == [100.0, None, 80.0, None]
    assert col(out, "prior_net_sales") == [90.0, 70.0, None, None]
    assert col(out, "net_sales_change") == [0.1, None, None, None]  # either side small
    assert col(out, "net_sales_delta") == [10.0, None, None, None]
    assert col(out, "n_orders") == [100, 20, 90, 5]  # the sample size stays visible
    assert col(r, "net_sales") == [100.0, 20.0, 80.0, 5.0]


def test_suppress_small_n_of_chosen_metrics_and_without_a_comparison(ctx):
    r = made(
        {"payment_type": ["big", "small"], "net_sales": [100.0, 20.0], "orders": [10, 5],
         "n_orders": [100, 20]},
        ["net_sales", "orders"], ["payment_type"],
    )  # fmt: skip
    out = run("suppress_small_n", r, ctx, metrics=["net_sales"])
    assert col(out, "net_sales") == [100.0, None] and col(out, "orders") == [10, 5]
    with pytest.raises(TransformError, match="gross_sales is not a metric"):
        run("suppress_small_n", r, ctx, metrics=["gross_sales"])


def test_suppress_small_n_on_a_real_result_blanks_everything_on_the_fixture(con, ctx):
    r = q(con, model="fct_orders", dimensions=["payment_type"], window="full",
          metrics=["net_sales", "food_attach_rate"])  # fmt: skip
    out = run("suppress_small_n", r, ctx)  # 4 orders in all: every row is under 30
    assert out.df["net_sales"].isna().all() and out.df["food_attach_rate"].isna().all()
    assert col(out, "n_orders") == col(r, "n_orders")


def test_suppress_small_n_blanks_timestamps_too(con, ctx):
    r = q(con, model="fct_orders", dimensions=["payment_type"], window="full",
          metrics=["first_sale_ts"])  # fmt: skip
    assert r.df["first_sale_ts"].notna().sum() == 2  # Gopay only had a refunded receipt
    assert run("suppress_small_n", r, ctx).df["first_sale_ts"].isna().all()


# --- the loader's side: pipelines and the requirements on the query -----------------------------


def test_a_pipeline_runs_the_steps_in_order(con, ctx):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
          metrics=["net_sales"], totals=True)  # fmt: skip
    out = run_pipeline(r, [{"sort_limit": {"by": "net_sales", "n": 3}},
                           {"cumulative_share": {"metric": "net_sales"}},
                           {"rank": {"by": "net_sales"}}], ctx)  # fmt: skip
    assert len(out.df) == 4  # three categories and the total
    assert col(out, "rank") == [1, 1, 3, None]  # Coffee and Mains tie at 70,000, then Cakes
    with pytest.raises(TransformError, match="unknown transform"):
        run_pipeline(r, ["nope"], ctx)


def test_the_requirements_of_growth_add_the_bucket_before_and_the_metrics():
    query = Query(model="fct_orders", dimensions=["bucket"], metrics=["net_sales"], window="L6W")
    planned = plan_query(query, [{"growth_vs_previous": {"metrics": ["net_sales", "orders"]}}])
    assert planned.buckets_before == 1 and planned.metrics == ["net_sales", "orders"]
    assert query.buckets_before == 0 and query.metrics == ["net_sales"]  # the input is untouched
    more = query.model_copy(update={"buckets_before": 3})
    assert plan_query(more, ["growth_vs_previous"]).buckets_before == 3  # never lowered
    assert plan_query(query, ["per_trading_day"]) is query  # nothing to add


def test_the_requirements_of_cumulative_share_and_lifecycle():
    query = Query(model="fct_sales_lines", dimensions=["adjusted_item"], metrics=["orders"],
                  window="L3M", compare="prior")  # fmt: skip
    steps = [{"cumulative_share": {"metric": "net_sales"}},
             {"classify": {"mode": "lifecycle", "by": "items_sold"}}]  # fmt: skip
    planned = plan_query(query, steps)
    assert planned.metrics == ["orders", "net_sales", "items_sold"]
    no_compare = query.model_copy(update={"compare": "none"})
    with pytest.raises(TransformError, match="classify needs a comparison"):
        plan_query(no_compare, [{"classify": {"mode": "lifecycle", "by": "items_sold"}}])
    # the other classify modes do not
    assert plan_query(no_compare, [{"classify": {"mode": "quadrants", "x": "a", "y": "b"}}])


def test_the_requirements_of_pivot_are_dimensions_of_the_query():
    query = Query(model="fct_orders", dimensions=["weekday"], metrics=["orders"], window="L6W")
    with pytest.raises(TransformError, match="pivot needs the dimension hour_of_day"):
        plan_query(query, [{"pivot": {"rows": "weekday", "columns": "hour_of_day"}}])
    with pytest.raises(TransformError, match="pivot: rows: Field required"):
        plan_query(query, [{"pivot": {"columns": "hour_of_day"}}])
    with pytest.raises(TransformError, match="unknown transform"):
        plan_query(query, ["nope"])


# --- the real run --------------------------------------------------------------------------------


@real
@pytest.mark.parametrize("period", PERIODS)
def test_orders_by_hour_per_trading_day_through_the_transforms(real_run, period):  # noqa: F811
    lines = real_run[2]
    ctx = TransformContext(REG, real_run[1])
    r = rq(real_run, model="fct_sales_lines", dimensions=["hour_of_day"], window=period,
           metrics=["orders"])  # fmt: skip
    out = run_pipeline(r, ["fill_domain", "per_trading_day"], ctx)
    p = next(p for p in report_periods(ANCHOR) if p.key == period)
    orders = lines[lines["counts_as_order"] & p.mask(lines["sale_ts"])]
    per_hour = orders.groupby("hour_of_day")["receipt_nr"].nunique()
    hours = range(per_hour.index.min(), per_hour.index.max() + 1)  # first to last, zeros between
    expected = per_hour.reindex(hours, fill_value=0) / orders["sale_date"].nunique()
    assert out.df["hour_of_day"].tolist() == list(hours)
    assert same(out.df["orders"], expected)
