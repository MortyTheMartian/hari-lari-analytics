"""The query compiler (plan 7.9): hand-computed results on the fixture.

The fixture, up to Tue 13 Oct 2026 (trading days: Mon 5, Mon 12 and Tue 13 Oct):

    R001 Mon 05 Oct 07:00 BCA    Latte (Coffee) 40,000 + Focaccia (Snacks) 28,000
    R002 Mon 05 Oct 08:00 Cash   Latte (Coffee) 40,000 - 10,000 FOUNDER25 discount
    R003 Tue 06 Oct 09:00 Gopay  Cappucino (Coffee) 40,000, fully refunded: not an order
    R004 Mon 12 Oct 10:00 BCA    2 x Nasi Campur (Mains) 35,000 + Mystery Cake (Cakes) 30,000
    R005 Tue 13 Oct 11:00 BCA    Custom Amount 15,000
"""

from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.reporting.checks import check
from hari_lari_analytics.reporting.compiler import Query, QueryError, compile_query, run_query
from hari_lari_analytics.reporting.db import connect, open_run
from hari_lari_analytics.reporting.inputs import ApiRun
from hari_lari_analytics.reporting.queries import (
    QueryContext,
    SourceQuery,
    execute,
    parse_query,
)
from hari_lari_analytics.reporting.queries.lines_export import EXPORT_COLUMNS
from hari_lari_analytics.reporting.registry import default_registry
from hari_lari_analytics.reporting.windows import Calendar

from .api_fixture import api_tables
from .test_models import LAST_SALE, REAL_RUN, SEEDS, _comparable

REG = default_registry()


@pytest.fixture(scope="module")
def con():
    return connect(api_tables(), SEEDS, LAST_SALE)


def q(con, **query):
    return run_query(con, Query(**query), REG)


def listed(column: pd.Series) -> list:
    return [None if pd.isna(v) else v for v in column]


def by(result, dim: str) -> dict:
    """The non-total rows keyed by one dimension (NULL as None)."""
    rows = result.rows
    keys = listed(rows[dim])
    return dict(zip(keys, rows.itertuples(), strict=True))


# --- one row ------------------------------------------------------------------------------------


def test_one_row_with_the_prior_week(con):
    # WTD: Mon 12 and Tue 13 Oct, against Mon 5 and Tue 6 Oct
    r = q(con, model="fct_orders", metrics=["net_sales", "orders", "aov"], window="WTD",
          compare="prior")  # fmt: skip
    row = r.df.iloc[0]
    assert len(r.df) == 1 and row.is_total and r.meta.shape == "single"
    assert (row.net_sales, row.orders, row.aov) == (115_000, 2, 57_500)
    assert (row.prior_net_sales, row.prior_orders, row.prior_aov) == (98_000, 2, 49_000)
    assert row.net_sales_change == pytest.approx(115_000 / 98_000 - 1)
    assert row.net_sales_delta == 17_000
    assert row.orders_change == 0
    assert (row.n_orders, row.prior_n_orders) == (2, 2)
    assert r.meta.compare_ok and not r.meta.empty
    assert (r.meta.trading_days, r.meta.prior_trading_days) == (2, 1)  # 6 Oct only had R003


def test_both_models_give_the_same_totals(con):
    metrics = ["gross_sales", "discounts", "refunds", "net_sales", "orders", "items_sold", "aov"]
    rows = [q(con, model=m, metrics=metrics, window="full").df.iloc[0]
            for m in ("fct_sales_lines", "fct_orders")]  # fmt: skip
    assert rows[0][metrics].tolist() == rows[1][metrics].tolist()
    assert rows[0][metrics].tolist() == [263_000, 10_000, 40_000, 213_000, 4, 7, 213_000 / 4]


def test_derived_metrics_use_the_trading_days_and_filtered_metrics(con):
    r = q(con, model="fct_orders", metrics=["net_sales_per_trading_day", "food_attach_rate"],
          window="WTD", compare="prior")  # fmt: skip
    row = r.df.iloc[0]
    assert row.net_sales_per_trading_day == 115_000 / 2
    assert row.prior_net_sales_per_trading_day == 98_000 / 1
    # orders with a drink: none this week, R001 and R002 (without food) the week before
    assert math.isnan(row.food_attach_rate) and row.prior_food_attach_rate == 0
    assert (row.n_drink_orders, row.prior_n_drink_orders) == (0, 2)  # the min_n of the metric
    assert set(r.meta.metrics) == {"net_sales_per_trading_day", "food_attach_rate",
                                   "n_orders", "n_drink_orders"}  # fmt: skip


def test_an_empty_window(con):
    r = q(con, model="fct_orders", metrics=["net_sales", "orders", "aov"], window="L3M")
    row = r.df.iloc[0]
    assert r.meta.empty and (row.net_sales, row.orders) == (0, 0) and math.isnan(row.aov)
    grouped = q(con, model="fct_orders", dimensions=["payment_type"], metrics=["net_sales"],
                window="L3M", totals=True)  # fmt: skip
    assert grouped.meta.empty and len(grouped.df) == 1 and grouped.df.iloc[0].net_sales == 0


def test_a_comparison_before_the_first_sale_is_null(con):
    r = q(con, model="fct_orders", metrics=["net_sales"], window="L14D", compare="prior")
    assert not r.meta.compare_ok and r.meta.has_comparison
    row = r.df.iloc[0]
    assert row.net_sales == 213_000
    assert all(pd.isna(row[c]) for c in ["prior_net_sales", "net_sales_change", "prior_n_orders"])
    assert "prior_r" not in r.meta.sql  # the comparison is not run at all


# --- groups, totals and prior-only groups -------------------------------------------------------


def test_categories_with_totals_and_prior_only_groups(con):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="WTD",
          metrics=["net_sales", "share", "orders", "penetration", "category_aov",
                   "contributive_aov"],
          compare="prior", totals=True, keep_prior_only=True)  # fmt: skip
    rows = by(r, "adjusted_category")
    # sorted by net sales; the categories sold only in the prior week come last with zeros
    assert list(rows) == ["Mains", "Cakes", "Custom Amount", "Coffee", "Snacks (Focaccia)"]
    mains, coffee = rows["Mains"], rows["Coffee"]
    assert (mains.net_sales, mains.orders, mains.penetration) == (70_000, 1, 0.5)
    assert (mains.category_aov, mains.contributive_aov) == (70_000, 35_000)
    assert (coffee.net_sales, coffee.orders, coffee.penetration, coffee.contributive_aov) == (
        0,
        0,
        0,
        0,
    )
    assert (coffee.prior_net_sales, coffee.prior_orders, coffee.prior_penetration) == (
        70_000, 2, 1.0
    )  # fmt: skip
    assert coffee.contributive_aov_delta == -35_000
    total = r.total
    assert (total.net_sales, total.orders, total.penetration, total.share) == (115_000, 2, 1, 1)
    assert total.category_aov == total.contributive_aov == 57_500  # the AOV
    assert r.rows["contributive_aov"].sum() == pytest.approx(total.contributive_aov)
    assert r.rows["prior_contributive_aov"].sum() == pytest.approx(total.prior_contributive_aov)


def test_without_keep_prior_only_and_without_totals(con):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="WTD",
          metrics=["net_sales", "penetration"], compare="prior")  # fmt: skip
    assert list(r.df["adjusted_category"]) == ["Mains", "Cakes", "Custom Amount"]
    assert not r.df["is_total"].any()
    assert r.total is None


def test_orders_are_distinct_per_group_and_in_total(con):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
          metrics=["orders"], totals=True)  # fmt: skip
    assert r.rows["orders"].sum() == 6  # R001 and R004 have two categories each
    assert r.total.orders == 4


def test_top_n_with_other(con):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
          metrics=["net_sales", "orders"], top={"n": 1, "by": "net_sales", "other": True},
          totals=True)  # fmt: skip
    # Coffee and Mains tie at 70,000: the name decides
    assert listed(r.df["adjusted_category"]) == ["Coffee", "Other (4)", None]
    other = r.df.iloc[1]
    assert other.is_other and other.net_sales == 143_000
    assert other.orders == 3  # R001, R004, R005: distinct receipts, not 1 + 1 + 1 + 1
    assert r.total.net_sales == 213_000


def test_other_never_hides_a_single_group(con):
    # five categories: the top 4 would leave one, which keeps its name instead of "Other (1)"
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
          metrics=["net_sales"], top={"n": 4, "by": "net_sales", "other": True})  # fmt: skip
    assert len(r.rows) == 5 and not r.rows["is_other"].any()
    three = q(con, model="fct_sales_lines", dimensions=["adjusted_category"], window="full",
              metrics=["net_sales"], top={"n": 3, "by": "net_sales", "other": True})  # fmt: skip
    assert listed(three.rows["adjusted_category"])[-1] == "Other (2)"


def test_top_n_without_other_keeps_the_total_of_everything(con):
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_item_variant", "adjusted_category"],
          window="full", metrics=["net_sales", "share"], top={"n": 2, "by": "net_sales"},
          totals=True)  # fmt: skip
    rows = r.rows
    assert rows["adjusted_item_variant"].tolist() == ["Nasi Campur", "Latte-Hot"]
    assert rows["adjusted_category"].tolist() == ["Mains", "Coffee"]
    assert rows["share"].tolist() == pytest.approx([70_000 / 213_000, 40_000 / 213_000])
    assert r.total.net_sales == 213_000


def test_the_prior_of_other_holds_everything_outside_the_top(con):
    r = q(
        con,
        model="fct_sales_lines",
        dimensions=["adjusted_category"],
        window="WTD",
        metrics=["net_sales"],
        compare="prior",
        top={"n": 1, "by": "net_sales", "other": True},
    )
    rows = by(r, "adjusted_category")
    assert (rows["Mains"].net_sales, rows["Mains"].prior_net_sales) == (70_000, 0)
    # Cakes and Custom Amount now; Coffee and Snacks (not in this week's ranking) before
    assert (rows["Other (2)"].net_sales, rows["Other (2)"].prior_net_sales) == (45_000, 98_000)


def test_prior_rank_is_the_rank_in_the_prior_window_among_all_groups(con):
    # this week only BCA paid; the week before BCA (68,000), Cash (30,000) and Gopay (0) did
    kwargs = dict(model="fct_orders", dimensions=["payment_type"], metrics=["net_sales"],
                  window="WTD", compare="prior")  # fmt: skip
    r = q(con, top={"n": 1, "by": "net_sales"}, **kwargs)
    assert r.df["payment_type"].tolist() == ["BCA"] and r.df["prior_rank"].tolist() == [1]
    assert r.meta.top_n == 1
    # a group of this week's top that had no rows the week before has no prior rank
    r = q(con, model="fct_sales_lines", dimensions=["adjusted_category"],
          metrics=["net_sales"], window="WTD", compare="prior",
          top={"n": 2, "by": "net_sales"})  # fmt: skip
    assert r.df["adjusted_category"].tolist() == ["Mains", "Cakes"]
    assert r.df["prior_rank"].isna().all()


def test_prior_rank_needs_top_and_a_comparison(con):
    kwargs = dict(model="fct_orders", dimensions=["payment_type"], metrics=["net_sales"])
    assert "prior_rank" not in q(con, window="WTD", compare="prior", **kwargs).df
    assert "prior_rank" not in q(con, window="WTD", top={"n": 1, "by": "net_sales"}, **kwargs).df
    # an invalid comparison (it starts before the first sale) gives a column of nulls
    r = q(con, window="L14D", compare="prior", top={"n": 1, "by": "net_sales"}, **kwargs)
    assert not r.meta.compare_ok and r.df["prior_rank"].isna().all()


def test_bins_from_the_registry_and_from_the_query(con):
    r = q(con, model="fct_orders", dimensions=["basket_size"], window="full",
          metrics=["net_sales", "orders"])  # fmt: skip
    # R003 has 1 - 1 = 0 items and falls outside every bin; bins sort in their own order
    assert listed(r.df["basket_size"]) == ["1", "2", "3", None]
    assert r.df["orders"].tolist() == [2, 1, 1, 0]
    r = q(con, model="fct_orders", dimensions=["basket_size"], window="full",
          metrics=["net_sales"], bins={"basket_size": [1, "2+"]})  # fmt: skip
    assert by(r, "basket_size")["2+"].net_sales == 168_000


def test_sort_by_value_with_null_label_values_last(con):
    r = q(con, model="fct_sales_lines", dimensions=["discount_code"], window="full",
          metrics=["gross_sales", "discounts", "discount_rate"])  # fmt: skip
    rows = by(r, "discount_code")
    assert list(rows) == [None, "FOUNDER25"]  # sorted by gross sales, as dimensions.yml says
    assert rows["FOUNDER25"].discount_rate == 0.25


# --- filters ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "where, net_sales, orders",
    [
        ({"payment_type": "BCA"}, 183_000, 3),
        ({"payment_type": ["Cash", "Gopay"]}, 30_000, 1),
        ({"has_drink": True}, 98_000, 2),
        ({"basket_size": "1"}, 45_000, 2),
        ({"basket_size": None}, 0, 0),
        ({"weekday": 2, "payment_type": "BCA"}, 15_000, 1),
    ],
)
def test_filters(con, where, net_sales, orders):
    r = q(con, model="fct_orders", metrics=["net_sales", "orders"], window="full", where=where)
    assert (r.df.iloc[0].net_sales, r.df.iloc[0].orders) == (net_sales, orders)


def test_a_null_filter_on_lines(con):
    r = q(con, model="fct_sales_lines", metrics=["net_sales"], window="full",
          where={"discount_code": None})  # fmt: skip
    assert r.df.iloc[0].net_sales == 183_000


def test_filter_values_are_bound_parameters(con):
    compiled = compile_query(
        Query(model="fct_orders", metrics=["orders"], window="full",
              where={"payment_type": ["BCA", "O'Brien"]}),
        REG,
        Calendar.from_db(con),
    )  # fmt: skip
    assert "BCA" not in compiled.sql and "O'Brien" not in compiled.sql
    assert "m.payment_type in ($w0, $w1)" in compiled.sql
    assert (compiled.params["w0"], compiled.params["w1"]) == ("BCA", "O'Brien")
    assert compiled.run(con).df.iloc[0].orders == 3


# --- trading days -------------------------------------------------------------------------------


def test_trading_days_per_weekday(con):
    r = q(con, model="fct_orders", dimensions=["weekday", "hour_of_day"], window="full",
          metrics=["orders"], totals=True)  # fmt: skip
    rows = r.rows
    assert rows[["weekday", "hour_of_day"]].values.tolist() == [
        [1, 7], [1, 8], [1, 10], [2, 9], [2, 11]
    ]  # fmt: skip
    # Mondays: 5 and 12 Oct; Tuesdays: only 13 Oct (6 Oct had no order)
    assert rows["trading_days"].tolist() == [2, 2, 2, 1, 1]
    assert r.total.trading_days == 3


def test_trading_days_of_other_groups_are_those_of_the_window(con):
    r = q(con, model="fct_orders", dimensions=["payment_type"], window="full", metrics=["orders"])
    assert set(r.df["trading_days"]) == {3}


def test_a_filter_on_a_calendar_dimension_restricts_the_trading_days(con):
    r = q(con, model="fct_orders", metrics=["net_sales_per_trading_day"], window="full",
          where={"weekday": 1})  # fmt: skip
    assert r.meta.trading_days == 2
    assert r.df.iloc[0].net_sales_per_trading_day == (68_000 + 30_000 + 100_000) / 2


# --- series -------------------------------------------------------------------------------------


def test_a_daily_series_with_its_prior_and_a_bucket_before(con):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="WTD", compare="prior",
          metrics=["net_sales", "orders"], buckets_before=1, totals=True)  # fmt: skip
    df = r.df
    assert df["bucket"].dt.date.tolist()[:3] == [date(2026, 10, 11), date(2026, 10, 12),
                                                 date(2026, 10, 13)]  # fmt: skip
    assert df["in_window"].tolist() == [False, True, True, True]
    assert df["net_sales"].tolist() == [0, 100_000, 15_000, 115_000]
    # the prior is matched by position: Mon 12 with Mon 5, Tue 13 with Tue 6
    assert df["prior_net_sales"].tolist()[1:] == [98_000, 0, 98_000]
    assert pd.isna(df["prior_net_sales"].iloc[0])  # no prior for the bucket before
    assert df["trading_days"].tolist() == [0, 1, 1, 2]
    assert r.meta.shape == "series" and r.meta.grain == "day"
    assert [b.ordinal for b in r.meta.buckets] == [-1, 0, 1]
    assert [b.start for b in r.meta.prior_buckets] == [date(2026, 10, 5), date(2026, 10, 6)]


def test_no_bucket_before_the_first_sale(con):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="L2W", metrics=["net_sales"],
          buckets_before=1)  # fmt: skip
    # weeks of 28 Sep and 5 Oct; the week of 21 Sep lies before the first sale (5 Oct)
    assert r.df["bucket"].dt.date.tolist() == [date(2026, 9, 28), date(2026, 10, 5)]
    assert r.df["net_sales"].tolist() == [0, 98_000]
    assert r.df["in_window"].all()


def test_seven_day_blocks_count_back_from_the_window_end(con):
    r = q(con, model="fct_orders", dimensions=["bucket"], window="L14D", grain="block_7d",
          metrics=["net_sales", "orders"])  # fmt: skip
    assert r.df["bucket"].dt.date.tolist() == [date(2026, 9, 30), date(2026, 10, 7)]
    assert r.df["net_sales"].tolist() == [98_000, 115_000]


def test_a_calendar_dimension_brings_its_own_grain(con):
    r = q(con, model="fct_orders", dimensions=["sale_month"], window="full", metrics=["orders"])
    assert r.meta.grain == "month" and r.df["sale_month"].dt.date.tolist() == [date(2026, 10, 1)]


def test_a_series_by_group_fills_every_bucket_and_uses_bucket_totals(con):
    r = q(con, model="fct_sales_lines", dimensions=["bucket", "adjusted_category"], window="WTD",
          metrics=["net_sales", "penetration"], totals=True)  # fmt: skip
    df = r.df.assign(day=r.df["bucket"].dt.day)
    members = df[~df["is_total"]]
    # every category of the window in every bucket, zeros included
    assert len(members) == 2 * 3
    cell = members.set_index(["day", "adjusted_category"])
    assert cell.loc[(12, "Mains"), "penetration"] == 1.0  # of the orders of 12 Oct
    assert cell.loc[(13, "Custom Amount"), "penetration"] == 1.0
    assert cell.loc[(12, "Custom Amount"), "net_sales"] == 0
    bucket_totals = df[df["is_total"] & df["bucket"].notna()]
    assert bucket_totals["net_sales"].tolist() == [100_000, 15_000]
    assert r.total.net_sales == 115_000


# --- invalid queries ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, message",
    [
        ({"metrics": ["revenue"]}, "unknown metric 'revenue'"),
        ({"dimensions": ["adjusted_category"]}, "adjusted_category is not on fct_orders"),
        ({"dimensions": ["bucket", "sale_week"]}, "at most one time dimension"),
        ({"grain": "week"}, "grain needs a time dimension"),
        ({"dimensions": ["sale_week"], "grain": "month"}, "contradicts the dimension sale_week"),
        ({"keep_prior_only": True}, "keep_prior_only needs a comparison"),
        ({"top": {"n": 3, "by": "net_sales"}}, "top needs a dimension"),
        ({"window": "$period"}, "the parameter $period is not bound"),
        ({"window": "L2Y"}, "window: unknown window"),
        ({"where": {"bucket": "2026-10-01"}}, "cannot filter on bucket"),
        ({"where": {"payment_type": "$payment_type"}}, "a parameter is not bound"),
        ({"where": {"basket_size": "9"}}, "are not bins of basket_size"),
        ({"bins": {"basket_size": [1]}}, "basket_size is not a dimension of the query"),
        ({"buckets_before": 1}, "buckets_before needs a time dimension"),
        (
            {"dimensions": ["payment_type"], "top": {"n": 1, "by": "share"}},
            "cannot rank by the relative metric share",
        ),
    ],
)
def test_invalid_queries(con, query, message):
    base = {"model": "fct_orders", "metrics": ["net_sales"], "window": "full"}
    with pytest.raises(QueryError, match=re.escape(message)):
        compile_query(Query(**{**base, **query}), REG, Calendar.from_db(con))


def test_unknown_keys_fail():
    with pytest.raises(QueryError, match="^query: .*metrcis: Extra inputs are not permitted"):
        parse_query({"model": "fct_orders", "metrcis": ["orders"], "window": "full"})


# --- snapshots and sort orders ------------------------------------------------------------------


def test_a_snapshot_model_is_queried_without_a_window(con):
    # fct_stock of the fixture: Latte-Hot out (0), Latte-Cold low (3), Cappucino-Hot ok (40),
    # Focaccia Tomato negative (-7)
    r = q(con, model="fct_stock", dimensions=["stock_status"], metrics=["stock_level"])
    assert r.meta.window is None
    # in the order of the dimension's values, not alphabetically; the sort metric of the item
    # grain (net sales) is not on fct_stock, so the values sort by themselves
    assert listed(r.rows["stock_status"]) == ["negative", "out", "low", "ok"]
    assert listed(r.rows["stock_level"]) == [-7, 0, 3, 40]
    alert = q(con, model="fct_stock", dimensions=["adjusted_item_variant"],
              metrics=["stock_level"],
              where={"stock_status": ["negative", "out", "low"]})  # fmt: skip
    assert listed(alert.rows["adjusted_item_variant"]) == [
        "Focaccia Tomato", "Latte-Cold", "Latte-Hot",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({"model": "fct_stock", "window": "L6W"}, "window: fct_stock has no time column"),
        ({"model": "fct_stock", "compare": "prior"}, "compare: fct_stock is a snapshot"),
        ({"model": "fct_orders", "metrics": ["orders"]}, "the query needs a window"),
    ],
)
def test_snapshot_and_time_rules(con, query, message):
    with pytest.raises(QueryError, match=message):
        compile_query(Query(**{"metrics": ["stock_level"], **query}), REG, Calendar.from_db(con))


# --- @query sources -----------------------------------------------------------------------------


def test_the_lines_export_has_the_excel_columns_and_never_the_note(con):
    ctx = QueryContext.create(con)
    r = execute(parse_query({"source": "lines_export"}), ctx)
    assert list(r.df.columns) == EXPORT_COLUMNS and "note" not in r.df.columns
    lines = con.execute("select * from fct_sales_lines").df()
    pd.testing.assert_frame_equal(
        _comparable(r.df, EXPORT_COLUMNS), _comparable(lines, EXPORT_COLUMNS), check_dtype=False
    )


def test_the_lines_export_orders_the_lines_of_one_receipt_and_second_by_their_key(con):
    r = execute(parse_query({"source": "lines_export"}), QueryContext.create(con))
    expected = con.execute(
        "select item from fct_sales_lines order by sale_ts, receipt_nr, payment_id, line_uuid"
    ).fetchall()
    assert r.df["item"].tolist() == [item for (item,) in expected]
    assert r.df.loc[r.df["receipt_nr"] == "R004", "item"].tolist() == [
        "Nasi Campur", "Nasi Campur", "Mystery Cake"
    ]  # fmt: skip


def test_the_data_notes(con):
    run = ApiRun("data/extract/2026-10-13", {}, {"warnings": ["last sale is old"]})
    ctx = QueryContext.create(con, run=run, checks=check(con))
    r = execute(SourceQuery(source="notes"), ctx)
    assert r.df.iloc[0].tolist() == ["source", "Moka API extraction 2026-10-13"]
    assert r.df.iloc[1].tolist() == ["extraction", "Extraction warning: last sale is old"]
    assert set(r.df["kind"]) == {"source", "extraction", "check"}  # the unmapped categories


def test_unknown_sources_and_options_fail(con):
    ctx = QueryContext.create(con)
    with pytest.raises(QueryError, match="unknown source 'nope'"):
        execute(SourceQuery(source="nope"), ctx)
    with pytest.raises(QueryError, match="options of notes"):
        execute(SourceQuery(source="notes", options={"x": 1}), ctx)


def test_execute_runs_a_compiled_query(con):
    ctx = QueryContext.create(con)
    r = execute(parse_query({"model": "fct_orders", "metrics": ["orders"], "window": "full"}), ctx)
    assert r.df.iloc[0].orders == 4


# --- the real run, for the tests of the transforms, facets, focus and decompose ----------------

ANCHOR = date(2026, 11, 22)
real = pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run")
PERIODS = ["L14D", "L6W", "L3M"]


@pytest.fixture(scope="module")
def real_run():
    """The models of the real run of 2026-11-22: the connection, the calendar and the lines."""
    db = open_run(REAL_RUN, Path("config"), ANCHOR)
    lines = db.con.execute("select * from fct_sales_lines").df()
    return db.con, Calendar.from_db(db.con), lines


def rq(real_run, **query):
    con, calendar, _ = real_run
    return run_query(con, Query(**query), REG, calendar)


def same(a, b) -> bool:
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=1e-12)
