"""The port of today's report (plan 9.3): every packaged component that replaces a piece of
`report_pdf.py` gives the numbers of `report_data.Section` (or of the cover's own figures).

The drawing may differ, so only the results are compared. Each test runs on the fixture run and,
when it is present, on the real run of 2026-11-22; on the fixture, L14D has no comparison and
L3M has no sales, so the empty and the comparison-less cases are covered too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.load_api import ApiRun
from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.report_data import Section, build_section
from hari_lari_analytics.report_pdf import MAX_PAYMENT_TYPES
from hari_lari_analytics.reporting.checks import check
from hari_lari_analytics.reporting.components import load_components
from hari_lari_analytics.reporting.db import connect, open_run
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.result import Result

from .api_fixture import api_tables
from .test_models import LAST_SALE, REAL_RUN, SEEDS, _comparable

COMPONENTS = load_components()
PERIODS = ["L14D", "L6W", "L3M"]
REAL_ANCHOR = date(2026, 11, 22)
real = pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run")


@dataclass
class Port:
    ctx: QueryContext
    lines: pd.DataFrame
    sections: dict[str, Section]

    def run(self, component: str, period: str | None = None) -> list:
        params = {"period": period} if period else None
        return run_facets(COMPONENTS[component], self.ctx, params)

    def result(self, component: str, period: str | None = None) -> Result:
        (facet,) = self.run(component, period)
        return facet.result


def _port(con, run: ApiRun, anchor: date) -> Port:
    lines = con.execute("select * from fct_sales_lines").df()
    first = lines["sale_ts"].min().date()
    sections = {p.key: build_section(lines, p, first) for p in report_periods(anchor)}
    return Port(QueryContext.create(con, run=run, checks=check(con)), lines, sections)


@pytest.fixture(scope="module", params=["fixture", pytest.param("real", marks=real)])
def port(request) -> Port:
    if request.param == "fixture":
        run = ApiRun("data/extract/2026-10-14", {}, {"warnings": ["last sale is old"]})
        return _port(connect(api_tables(), SEEDS, LAST_SALE), run, LAST_SALE)
    db = open_run(REAL_RUN, Path("config"), REAL_ANCHOR)
    return _port(db.con, db.run, REAL_ANCHOR)


def same(a, b) -> bool:
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=1e-12)


def values(column: pd.Series) -> list:
    return [None if pd.isna(v) else v for v in column]


# --- the cover ----------------------------------------------------------------------------------


def test_full_history_cards_amounts_and_last_sale(port):
    t = compute_metrics(port.lines).iloc[0]
    cards = port.run("full_history_kpis")
    assert [f.metric for f in cards] == ["net_sales", "orders", "aov", "items_sold"]
    row = cards[0].result.df.iloc[0]
    assert same([row[m] for m in ["net_sales", "orders", "aov", "items_sold"]],
                [t.net_sales, t.orders, t.aov, t.items_sold])  # fmt: skip
    amounts = port.result("full_history_amounts").df.iloc[0]
    assert same([amounts.gross_sales, amounts.discounts, amounts.refunds],
                [t.gross_sales, t.discounts, t.refunds])  # fmt: skip
    assert port.result("last_sale").df.iloc[0].last_sale_ts == port.lines["sale_ts"].max()


def test_period_cards_with_sparklines(port):
    facets = port.run("period_summaries")
    assert [f.key for f in facets] == PERIODS
    for facet in facets:
        s, r = port.sections[facet.key], facet.result
        assert facet.label == s.period.title and r.meta.window.current.label == s.period.label()
        assert r.meta.compare_ok == s.prior_ok and r.meta.empty == s.empty
        kpis = s.kpis.set_index("key")
        total = r.total
        for m in ["net_sales", "orders"]:
            assert same([total[m]], [kpis.loc[m, "current"]]), (facet.key, m)
            if s.prior_ok:
                assert same([total[f"{m}_change"]], [kpis.loc[m, "change"]]), (facet.key, m)
        assert r.rows["bucket"].tolist() == s.spark.index.tolist()
        assert same(r.rows["net_sales"], s.spark["net_sales"])


def test_the_data_notes_from_the_run_manifest(port):
    notes = port.result("data_notes").df
    run = port.ctx.run
    assert notes.iloc[0].tolist() == ["source", f"Moka API extraction {run.name}"]
    expected = [f"Extraction warning: {w}" for w in run.manifest.get("warnings", [])]
    assert notes[notes["kind"] == "extraction"]["note"].tolist() == expected
    assert notes[notes["kind"] == "check"]["note"].tolist() == [
        w.describe() for w in port.ctx.checks.warnings
    ]


# --- the overview page --------------------------------------------------------------------------


@pytest.mark.parametrize("period", PERIODS)
def test_kpi_cards_with_change_prior_and_sparkline(port, period):
    s = port.sections[period]
    cards = port.run("period_kpis", period)
    keys = ["net_sales", "orders", "aov", "items_sold"]
    assert [f.metric for f in cards] == keys
    r = cards[0].result
    assert r.meta.compare_ok == s.prior_ok
    kpis = s.kpis.set_index("key")
    total = r.total
    for m in keys:
        assert same([total[m]], [kpis.loc[m, "current"]]), m
        if s.prior_ok:
            assert same([total[f"prior_{m}"]], [kpis.loc[m, "prior"]]), m
            assert same([total[f"{m}_change"]], [kpis.loc[m, "change"]]), m
        spark = r.rows[m].astype(float).fillna(0)  # an empty block has no AOV; today draws 0
        assert same(spark, s.spark[m]), m


@pytest.mark.parametrize("period", PERIODS)
def test_net_sales_and_orders_trend_with_prior_and_growth(port, period):
    s = port.sections[period]
    r = port.result("sales_trend", period)
    t = s.trend
    assert r.df["bucket"].tolist() == t["bucket"].tolist()
    for c in ["net_sales", "orders", "net_sales_growth", "orders_growth"]:
        assert same(r.df[c].astype(float), t[c]), c
    if s.prior_ok:
        for c in ["prior_net_sales", "prior_orders"]:
            assert same(r.df[c].astype(float), t[c]), c
    assert r.meta.compare_ok == s.prior_ok


@pytest.mark.parametrize("period", PERIODS)
def test_key_figures(port, period):
    s = port.sections[period]
    row = port.result("key_figures", period).df.iloc[0]
    keys = s.kpis["key"].tolist()
    assert same([row[k] for k in keys], s.kpis["current"])
    if s.prior_ok:
        assert same([row[f"prior_{k}"] for k in keys], s.kpis["prior"])
        assert same([row[f"{k}_change"] for k in keys], s.kpis["change"])
    if not s.empty:
        assert row.first_sale_ts == s.times["first"] and row.last_sale_ts == s.times["last"]


# --- the categories page ------------------------------------------------------------------------


@pytest.mark.parametrize("period", PERIODS)
def test_categories_and_contributive_aov(port, period):
    s = port.sections[period]
    r = port.result("category_contributive_aov", period)
    rows, cats = r.rows, s.categories
    assert rows["adjusted_category"].tolist() == cats["adjusted_category"].tolist()
    columns = ["net_sales", "items_sold", "orders", "share", "penetration", "category_aov",
               "contributive_aov"]  # fmt: skip
    if s.prior_ok:
        columns += ["prior_penetration", "prior_category_aov", "prior_contributive_aov",
                    "category_aov_change", "contributive_aov_delta"]  # fmt: skip
        assert same(rows["penetration_delta"] * 100, cats["delta_pp"])
    for c in columns:
        assert same(rows[c], cats[c]), c
    if not s.empty:
        for c in ["net_sales", "items_sold", "orders", "share", "penetration", "category_aov",
                  "contributive_aov"]:  # fmt: skip
            assert same([r.total[c]], [s.category_total[c]]), c


@pytest.mark.parametrize("period", PERIODS)
def test_penetration_dot_plot(port, period):
    s = port.sections[period]
    rows = port.result("category_penetration", period).rows
    assert rows["penetration"].is_monotonic_decreasing
    ours = rows.set_index("adjusted_category")
    theirs = s.categories.set_index("adjusted_category")
    sold = theirs[theirs["net_sales"] != 0]  # today keeps the prior-only rows; the plot not
    assert set(ours.index) == set(sold.index)
    assert same(ours.loc[sold.index, "penetration"], sold["penetration"])
    if s.prior_ok:
        prior = ours.loc[sold.index, "prior_penetration"].astype(float).fillna(0)
        assert same(prior, sold["prior_penetration"])


# --- the items, hours and payments page ---------------------------------------------------------


@pytest.mark.parametrize("period", PERIODS)
def test_top_15_items(port, period):
    s = port.sections[period]
    r = port.result("top_items", period)
    key = ["adjusted_item_variant", "adjusted_category"]
    expected = s.items.sort_values(["net_sales", *key], ascending=[False, True, True])
    assert r.rows[key].values.tolist() == expected[key].values.tolist()
    for c in ["items_sold", "net_sales", "share"]:
        assert same(r.rows[c], expected[c]), c


@pytest.mark.parametrize("period", PERIODS)
def test_orders_by_hour_per_trading_day(port, period):
    s = port.sections[period]
    r = port.result("orders_by_hour", period)
    assert r.rows["hour_of_day"].tolist() == s.hours["hour"].tolist()
    assert same(r.rows["orders"], s.hours["orders"])
    assert r.meta.trading_days == s.trading_days


def _today_payments(s: Section) -> pd.DataFrame:
    pay = s.payments[["payment_type", "orders", "net_sales", "share"]]
    if len(pay) <= MAX_PAYMENT_TYPES:
        return pay
    rest = pay.iloc[MAX_PAYMENT_TYPES - 1 :]
    other = {"payment_type": f"Other ({len(rest)})", "orders": rest["orders"].sum(),
             "net_sales": rest["net_sales"].sum(), "share": rest["share"].sum()}  # fmt: skip
    return pd.concat([pay.iloc[: MAX_PAYMENT_TYPES - 1], pd.DataFrame([other])])


@pytest.mark.parametrize("period", PERIODS)
def test_payment_types_with_other(port, period):
    s = port.sections[period]
    rows = port.result("payment_types", period).rows
    expected = _today_payments(s)
    assert rows["payment_type"].tolist() == expected["payment_type"].tolist()
    for c in ["orders", "net_sales", "share"]:
        assert same(rows[c], expected[c]), c


@pytest.mark.parametrize("period", PERIODS)
def test_discounts_by_code(port, period):
    s = port.sections[period]
    rows = port.result("discount_codes", period).rows
    assert rows["discount_code"].fillna("No discount").tolist() == s.discounts["code"].tolist()
    for ours, theirs in [("orders", "orders"), ("gross_sales", "gross_sales"),
                         ("discounts", "discounts"), ("discount_rate", "rate")]:  # fmt: skip
        assert same(rows[ours], s.discounts[theirs]), ours


# --- the workbook -------------------------------------------------------------------------------


def test_the_lines_export_has_the_lines_of_the_excel_file(port):
    from hari_lari_analytics.report_excel import COLUMNS

    df = port.result("lines_export").df
    assert list(df.columns) == [c for c, *_ in COLUMNS]
    columns = list(df.columns)
    pd.testing.assert_frame_equal(
        _comparable(df, columns), _comparable(port.lines, columns), check_dtype=False
    )
