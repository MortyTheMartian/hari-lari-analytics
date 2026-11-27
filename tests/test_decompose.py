"""The `decompose` transform (plan 7.11): hand-computed cases, the worked example of
docs/components.md on the run of 2026-11-22, and effects that add up on every identity.

The fixture of `test_compiler`, up to Tue 13 Oct 2026; WTD is Mon 12 and Tue 13 Oct against
Mon 5 and Tue 6 Oct:

    R001 Mon 05 Oct  Latte (Coffee) 40,000 + Focaccia (Snacks (Focaccia)) 28,000
    R002 Mon 05 Oct  Latte (Coffee) 40,000 - 10,000 discount
    R003 Tue 06 Oct  Cappucino (Coffee) 40,000, fully refunded: not an order
    R004 Mon 12 Oct  2 x Nasi Campur (Mains) 35,000 + Mystery Cake (Cakes) 30,000
    R005 Tue 13 Oct  Custom Amount 15,000
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from hari_lari_analytics.analysis.compiler import Query, run_query
from hari_lari_analytics.analysis.result import Result, ResultMeta
from hari_lari_analytics.analysis.transforms import (
    TransformContext,
    TransformError,
    apply,
    plan_query,
    run_pipeline,
)
from hari_lari_analytics.analysis.transforms.decompose import log_mean
from hari_lari_analytics.analysis.windows import Calendar, ResolvedWindow, Window
from hari_lari_analytics.semantic.registry import default_registry, identity_models
from hari_lari_analytics.warehouse.db import connect

from .api_fixture import api_tables
from .test_compiler import real, real_run  # noqa: F401  (real_run: a fixture)
from .test_models import LAST_SALE, SEEDS

REG = default_registry()
CTX = TransformContext(REG)


@pytest.fixture(scope="module")
def con():
    return connect(api_tables(), SEEDS, LAST_SALE)


def window(compare_ok: bool = True) -> ResolvedWindow:
    cur = Window(date(2026, 10, 12), date(2026, 10, 18), "day", "This week", 7)
    prior = Window(date(2026, 10, 5), date(2026, 10, 11), "day", "Prior week", 7)
    return ResolvedWindow("L7D", cur, "prior", prior, compare_ok, "prior 7 days")


def made(rows: list[dict], shape: str = "single", dimensions=(), compare_ok=True) -> Result:
    """A result by hand; each row gives current and `prior_` values, the metric specs come from
    the registry."""
    df = pd.DataFrame(rows)
    df["n_orders"] = 100
    df["prior_n_orders"] = 100
    names = {c.removeprefix("prior_") for c in df.columns} - {"is_total", "n_orders", *dimensions}
    metrics = {m: REG.metric_on(m, "fct_sales_lines") for m in sorted(names)}
    metrics["n_orders"] = REG.metric_on("orders", "fct_sales_lines")
    meta = ResultMeta(
        shape=shape,
        window=window(compare_ok),
        empty=False,
        metrics=metrics,
        dimensions={d: REG.dimension(d) for d in dimensions},
    )
    return Result(df, meta)


def decompose(result: Result, **options) -> Result:
    return apply("decompose", options, result, CTX)


def effects(result: Result, level: int = 1) -> dict:
    rows = result.df[result.df["level"] == level]
    key = result.meta.decomposition.dimension or "metric"
    return dict(zip(rows[key], rows["effect"], strict=True))


def adds_up(result: Result) -> None:
    """Level 1 adds up to the KPI's change, and the level 2 rows of a member to its effect."""
    df = result.df
    kpi = df[df["is_total"]].iloc[0]
    assert df.loc[df["level"] == 1, "effect"].sum() == pytest.approx(kpi.effect, rel=1e-9)
    dim = result.meta.decomposition.dimension
    for _, member in df[(df["level"] == 1) & df["status"].isna()].iterrows():
        parts = df[(df["level"] == 2) & (df[dim] == member[dim])] if dim else df.iloc[:0]
        if len(parts):
            assert parts["effect"].sum() == pytest.approx(member.effect, rel=1e-9)


# --- hand-computed --------------------------------------------------------------------------------


def test_log_mean():
    assert log_mean(5, 5) == 5
    assert log_mean(24, 6) == pytest.approx(18 / math.log(4))
    assert log_mean(6, 24) == log_mean(24, 6)


def test_a_product_splits_by_log_mean_weights():
    # AOV 6 = 2 items × 3 → 24 = 4 items × 6: L(24, 6) = 18 / ln 4, each driver doubles,
    # so each gets 18 / ln 4 × ln 2 = 9
    r = decompose(
        made([{"is_total": True, "aov": 24, "prior_aov": 6, "items_per_order": 4,
               "prior_items_per_order": 2, "avg_item_price": 6, "prior_avg_item_price": 3}]),
        identity="aov_basket",
    )  # fmt: skip
    assert r.meta.shape == "decomposition"
    assert r.meta.decomposition.kpi == "aov" and r.meta.decomposition.kind == "product"
    assert list(r.df["metric"]) == ["items_per_order", "avg_item_price", "aov"]
    assert list(r.df["level"]) == [1, 1, 0]
    assert list(r.df["effect"]) == pytest.approx([9, 9, 18])
    assert list(r.df["share_of_change"]) == pytest.approx([0.5, 0.5, 1])
    assert list(r.df["prior"]) == [2, 3, 6] and list(r.df["current"]) == [4, 6, 24]
    assert list(r.df["change"]) == [1, 1, 3] and list(r.df["delta"]) == [2, 3, 18]
    assert list(r.df["label"]) == ["Items per order", "Avg net price per item", "Avg Order Value"]
    assert list(r.df["n_orders"]) == [100, 100, 100]
    assert r.df["status"].isna().all()


def test_a_driver_that_does_not_move_gets_no_effect():
    # net sales 100 = 10 orders × 10 → 150 = 15 × 10: all of +50 comes from the orders
    r = decompose(
        made([{"is_total": True, "net_sales": 150, "prior_net_sales": 100, "orders": 15,
               "prior_orders": 10, "aov": 10, "prior_aov": 10}]),
        identity="sales_volume_value",
    )  # fmt: skip
    assert effects(r) == pytest.approx({"orders": 50, "aov": 0})


def test_a_sum_splits_by_signed_differences():
    r = decompose(
        made([{"is_total": True, "net_sales": 80, "prior_net_sales": 90, "gross_sales": 100,
               "prior_gross_sales": 95, "discounts": 15, "prior_discounts": 5, "refunds": 5,
               "prior_refunds": 0}]),
        identity="net_from_gross",
    )  # fmt: skip
    assert effects(r) == {"gross_sales": 5, "discounts": -10, "refunds": -5}
    assert list(r.df["sign"]) == [1, -1, -1, 1]
    assert r.df["share_of_change"].tolist() == pytest.approx([-0.5, 1, 0.5, 1])


def test_a_product_with_a_missing_or_zero_value_is_not_split():
    # no items in the prior period: no price per item, so the change of AOV cannot be split
    r = decompose(
        made([{"is_total": True, "aov": 24, "prior_aov": 6, "items_per_order": 4,
               "prior_items_per_order": 0, "avg_item_price": 6, "prior_avg_item_price": None}]),
        identity="aov_basket",
    )  # fmt: skip
    drivers = r.df[r.df["level"] == 1]
    assert drivers["effect"].isna().all() and (drivers["status"] == "undefined").all()
    assert r.df.iloc[-1].effect == 18


def test_without_a_valid_comparison_there_are_no_effects():
    r = decompose(
        made([{"is_total": True, "aov": 24, "prior_aov": None, "items_per_order": 4,
               "prior_items_per_order": None, "avg_item_price": 6, "prior_avg_item_price": None}],
             compare_ok=False),
        identity="aov_basket",
    )  # fmt: skip
    assert r.df["effect"].isna().all() and r.df["status"].isna().all()


def test_a_wrong_identity_fails():
    with pytest.raises(TransformError, match="does not hold"):
        decompose(
            made([{"is_total": True, "aov": 30, "prior_aov": 6, "items_per_order": 4,
                   "prior_items_per_order": 2, "avg_item_price": 6, "prior_avg_item_price": 3}]),
            identity="aov_basket",
        )  # fmt: skip


SLICES = [
    # A moves only by penetration, B only by category AOV, C is gone and D is new
    {"adjusted_category": "A", "penetration": 1.0, "prior_penetration": 0.5,
     "category_aov": 20, "prior_category_aov": 20},
    {"adjusted_category": "B", "penetration": 0.5, "prior_penetration": 0.5,
     "category_aov": 30, "prior_category_aov": 40},
    {"adjusted_category": "C", "penetration": 0.0, "prior_penetration": 0.1,
     "category_aov": None, "prior_category_aov": 50},
    {"adjusted_category": "D", "penetration": 0.25, "prior_penetration": 0.0,
     "category_aov": 40, "prior_category_aov": None},
]  # fmt: skip


def slices_result() -> Result:
    rows = []
    for row in SLICES:
        cur = row["penetration"] * (row["category_aov"] or 0)
        prior = row["prior_penetration"] * (row["prior_category_aov"] or 0)
        rows.append({**row, "is_total": False, "contributive_aov": cur,
                     "prior_contributive_aov": prior, "aov": 0, "prior_aov": 0})  # fmt: skip
    rows.append({"adjusted_category": None, "is_total": True, "aov": 45, "prior_aov": 35,
                 "contributive_aov": 45, "prior_contributive_aov": 35, "penetration": 1,
                 "prior_penetration": 1, "category_aov": 45, "prior_category_aov": 35})  # fmt: skip
    return made(rows, shape="rows", dimensions=["adjusted_category"])


def test_a_sum_over_has_one_row_per_member_largest_first():
    # slices A 10 → 20, B 20 → 15, C 5 → 0, D 0 → 10; AOV 35 → 45
    r = decompose(slices_result(), identity="aov_by_category")
    assert list(r.df["adjusted_category"].iloc[:-1]) == ["A", "D", "B", "C"]  # ties keep order
    assert effects(r) == pytest.approx({"A": 10, "D": 10, "B": -5, "C": -5})
    assert list(r.df["status"].fillna("-")) == ["-", "new", "-", "gone", "-"]
    assert list(r.df["label"].iloc[:-1]) == ["A", "D", "B", "C"]
    assert set(r.df["metric"].iloc[:-1]) == {"contributive_aov"}
    assert r.df.iloc[-1].effect == 10 and r.df.iloc[-1].metric == "aov"
    assert r.df["share_of_change"].iloc[:-1].tolist() == pytest.approx([1, 1, -0.5, -0.5])
    assert len(r.df[r.df["level"] == 2]) == 0
    adds_up(r)


def test_a_sum_over_in_query_order():
    r = decompose(slices_result(), identity="aov_by_category", order="query")
    assert list(r.df["adjusted_category"].iloc[:-1]) == ["A", "B", "C", "D"]


def test_depth_two_splits_each_member_by_its_slice():
    r = decompose(slices_result(), identity="aov_by_category", depth=2)
    assert r.meta.decomposition.then == "category_slice" and r.meta.decomposition.depth == 2
    df = r.df
    assert list(
        zip(df["adjusted_category"].fillna("total"), df["level"], df["metric"], strict=True)
    ) == [  # fmt: skip
        ("A", 1, "contributive_aov"),
        ("A", 2, "penetration"),
        ("A", 2, "category_aov"),
        ("D", 1, "contributive_aov"),  # new: shown whole
        ("B", 1, "contributive_aov"),
        ("B", 2, "penetration"),
        ("B", 2, "category_aov"),
        ("C", 1, "contributive_aov"),  # gone: shown whole
        ("total", 0, "aov"),
    ]
    level2 = df[df["level"] == 2]
    # A: L(20, 10) × ln 2 = 10 from penetration; B: L(15, 20) × ln 0.75 = -5 from category AOV
    assert level2["effect"].tolist() == pytest.approx([10, 0, 0, -5])
    assert level2["label"].tolist() == ["Order penetration", "Category AOV"] * 2
    adds_up(r)


def test_depth_two_needs_then():
    with pytest.raises(TransformError, match="has none"):
        decompose(slices_result(), identity="net_sales_by_category", depth=2)


def test_members_that_do_not_add_up_fail():
    result = slices_result()
    df = result.df.iloc[1:]  # A is missing
    with pytest.raises(TransformError, match="do not add up"):
        decompose(result.replace(df), identity="aov_by_category")


def test_the_shape_must_match_the_identity():
    with pytest.raises(TransformError, match="grouped by adjusted_category"):
        decompose(made([{"is_total": True, "aov": 1, "prior_aov": 1, "contributive_aov": 1,
                         "prior_contributive_aov": 1}]), identity="aov_by_category")  # fmt: skip
    with pytest.raises(TransformError, match="without dimensions"):
        decompose(slices_result(), identity="aov_basket")


def test_unknown_identity_and_options():
    with pytest.raises(TransformError, match="unknown KPI-tree identity"):
        decompose(slices_result(), identity="nope")
    with pytest.raises(TransformError, match="depth"):
        decompose(slices_result(), identity="aov_by_category", depth=3)


# --- the query it needs ---------------------------------------------------------------------------


def test_plan_query_adds_the_drivers():
    query = Query(model="fct_orders", metrics=["aov"], window="L6W", compare="prior")
    planned = plan_query(query, [{"decompose": {"identity": "aov_basket"}}])
    assert planned.metrics == ["aov", "items_per_order", "avg_item_price"]
    assert not planned.totals and not planned.keep_prior_only


def test_plan_query_adds_totals_and_prior_only_groups_for_a_sum_over():
    query = Query(model="fct_sales_lines", metrics=["net_sales"], window="L6W", compare="prior",
                  dimensions=["adjusted_category"])  # fmt: skip
    planned = plan_query(query, [{"decompose": {"identity": "aov_by_category", "depth": 2}}])
    assert planned.metrics == ["net_sales", "aov", "contributive_aov", "penetration",
                               "category_aov"]  # fmt: skip
    assert planned.totals and planned.keep_prior_only


@pytest.mark.parametrize(
    ("query", "step", "message"),
    [
        ({"metrics": ["aov"]}, {"identity": "aov_basket"}, "needs a comparison"),
        ({"metrics": ["aov"], "compare": "prior", "dimensions": ["payment_type"]},
         {"identity": "aov_basket"}, "needs a query grouped by no dimension"),
        ({"metrics": ["aov"], "compare": "prior"}, {"identity": "aov_by_category"},
         "grouped by adjusted_category, not by none"),
        ({"metrics": ["aov"], "compare": "prior", "dimensions": ["adjusted_category"],
          "top": {"n": 3, "by": "net_sales"}}, {"identity": "aov_by_category"},
         "set `other: true`"),
    ],
)  # fmt: skip
def test_plan_query_refuses_what_it_cannot_add(query, step, message):
    query = Query(model="fct_sales_lines", window="L6W", **query)
    with pytest.raises(TransformError, match=message):
        plan_query(query, [{"decompose": step}])


# --- on the fixture -------------------------------------------------------------------------------


def run(con, steps, **query) -> Result:
    calendar = Calendar.from_db(con)
    planned = plan_query(Query(**query), steps)
    return run_pipeline(run_query(con, planned, REG, calendar), steps, TransformContext(REG))


def test_categories_on_the_fixture_with_new_and_gone_members(con):
    # AOV 49,000 (98,000 / 2) → 57,500 (115,000 / 2): Mains and Cakes are new, Coffee and Snacks
    # gone (R003 was refunded), and the Custom Amount is new
    r = run(con, [{"decompose": {"identity": "aov_by_category", "depth": 2}}],
            model="fct_sales_lines", metrics=["aov"], dimensions=["adjusted_category"],
            window="WTD", compare="prior")  # fmt: skip
    assert effects(r) == {"Mains": 35_000, "Coffee": -35_000, "Cakes": 15_000,
                          "Snacks (Focaccia)": -14_000, "Custom Amount": 7_500}  # fmt: skip
    assert list(r.df["status"].iloc[:-1]) == ["new", "gone", "new", "gone", "new"]
    assert r.df.iloc[-1].effect == 8_500
    assert list(r.df["n_orders"]) == [1, 0, 1, 0, 1, 2]
    adds_up(r)


def test_top_with_other_adds_up(con):
    r = run(con, [{"decompose": {"identity": "net_sales_by_category"}}],
            model="fct_sales_lines", metrics=["net_sales"], dimensions=["adjusted_category"],
            window="WTD", compare="prior",
            top={"n": 2, "by": "net_sales", "other": True})  # fmt: skip
    assert r.df["is_other"].tolist() == [False, False, True, False]
    assert r.df.iloc[-2].label.startswith("Other (")
    adds_up(r)


def test_no_valid_comparison_on_the_fixture(con):
    r = run(con, [{"decompose": {"identity": "aov_basket"}}], model="fct_orders",
            metrics=["aov"], window="L14D", compare="prior")  # fmt: skip
    assert not r.meta.compare_ok and r.df["effect"].isna().all()


# --- on the real run ------------------------------------------------------------------------------


def real_decompose(real_run, steps, **query) -> Result:  # noqa: F811
    con, calendar, _ = real_run
    result = run_query(con, plan_query(Query(**query), steps), REG, calendar)
    return run_pipeline(result, steps, TransformContext(REG, calendar))


@real
def test_the_worked_example(real_run):  # noqa: F811
    """docs/components.md, "Worked example": the AOV in L6W up to 2026-11-22."""
    r = real_decompose(real_run, [{"decompose": {"identity": "aov_basket"}}], model="fct_orders",
                       metrics=["aov"], window="L6W", compare="prior")  # fmt: skip
    kpi = r.df.iloc[-1]
    assert (round(kpi.prior), round(kpi.current), round(kpi.effect)) == (67_129, 74_490, 7_361)
    assert log_mean(kpi.current, kpi.prior) == pytest.approx(70_746, abs=0.5)
    assert {k: round(v) for k, v in effects(r).items()} == {
        "items_per_order": 3_812,
        "avg_item_price": 3_549,
    }
    ipo = r.df.iloc[0]
    assert (round(ipo.prior, 2), round(ipo.current, 2)) == (1.94, 2.05)
    adds_up(r)

    r = real_decompose(real_run, [{"decompose": {"identity": "sales_volume_value"}}],
                       model="fct_orders", metrics=["net_sales"], window="L6W",
                       compare="prior")  # fmt: skip
    assert round(r.df.iloc[-1].effect) == 36_180_625
    assert {k: round(v) for k, v in effects(r).items()} == {
        "orders": 31_680_243,
        "aov": 4_500_382,
    }


@real
@pytest.mark.parametrize("period", ["L14D", "L6W", "L8W", "L1M", "WTD", "MTD"])  # with a prior
@pytest.mark.parametrize("identity", sorted(REG.identities))
def test_every_identity_adds_up_on_the_real_run(real_run, period, identity):  # noqa: F811
    spec = REG.identity(identity)
    for model in sorted(identity_models(REG, spec)):
        depth = 2 if spec.then else 1
        step = [{"decompose": {"identity": identity, "depth": depth}}]
        dims = [spec.dimension] if spec.dimension else []
        r = real_decompose(real_run, step, model=model, metrics=[spec.kpi], dimensions=dims,
                           window=period, compare="prior")  # fmt: skip
        assert r.meta.compare_ok
        if spec.kind == "product":
            assert r.df["status"].isna().all()
        adds_up(r)
