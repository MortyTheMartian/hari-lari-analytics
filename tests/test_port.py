"""The port of today's report (plan 9.3): every packaged component that replaces a piece of
`report_pdf.py` gives the numbers of `report_data.Section` (or of the cover's own figures), and
`reports/weekly.yml` (9.4) has today's pages, chips, footer and caption. The category deep dive
(9.5) is checked against the same sections.

The drawing may differ, so only the results are compared. Each test runs on the fixture run and,
when it is present, on the real run of 2026-11-22; on the fixture, L14D has no comparison and
L3M has no sales, so the empty and the comparison-less cases are covered too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.deliver.caption import build_caption
from hari_lari_analytics.load_api import ApiRun
from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.report_data import Section, build_section
from hari_lari_analytics.report_pdf import MAX_PAYMENT_TYPES
from hari_lari_analytics.reporting.builder import run_report
from hari_lari_analytics.reporting.checks import check
from hari_lari_analytics.reporting.components import load_components
from hari_lari_analytics.reporting.db import connect, open_run
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.reports import load_reports
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


# --- the weekly report (9.4): pages, chrome and caption ---------------------------------------


@pytest.fixture(scope="module")
def weekly(port):
    report = load_reports(COMPONENTS)["weekly"]
    return run_report(report, port.ctx, generated=GENERATED)


GENERATED = datetime(2026, 11, 25, 15, 0)


def _today_pages(port: Port) -> list[tuple[str, list[tuple[str, str]], str]]:
    """The title, chips and note of each page of today's PDF (`report_pdf.py`)."""
    first, anchor = port.lines["sale_ts"].min().date(), port.ctx.calendar.anchor
    pages = [("Sales report", [("Full history", "fill"),
                               (f"{first:%d %b %Y} – {anchor:%d %b %Y}", "line")], "")]  # fmt: skip
    for key in PERIODS:
        s = port.sections[key]
        chips = [(s.period.title, "fill"), (s.period.label(), "line")]
        if s.prior_ok:
            chips.append((f"vs {s.period.prior_label()}", "muted"))
            note = ""
        else:
            chips.append(("no comparison", "muted"))
            note = f"The prior period starts before the first sale ({first:%d %b %Y})."
        titles = ["Overview"] if s.empty else ["Overview", "Categories", "Items, hours & payments"]
        pages += [(title, chips, note) for title in titles]
    return pages


def test_the_weekly_pages_are_todays_in_todays_order_with_todays_chips(port, weekly):
    ours = [(p.page.title, p.chips, p.note) for p in weekly.pages]
    assert ours == _today_pages(port)


def test_an_empty_period_is_one_page_with_todays_message(port, weekly):
    empty = [p.page for p in weekly.pages if p.page.empty_message]
    expected = [k for k in PERIODS if port.sections[k].empty]
    assert [p.period for p in empty] == expected
    for page in empty:
        assert (page.title, page.empty_message) == ("Overview", "No sales in this period")


def test_the_footer_is_todays(port, weekly):
    anchor = port.ctx.calendar.anchor
    assert weekly.footer == (
        f"Hari Lari Cafe · Moka POS sales · data through {anchor:%d %b %Y} · "
        f"generated {GENERATED:%d %b %Y %H:%M}"
    )


def test_the_caption_has_the_content_of_todays(port, weekly):
    anchor = port.ctx.calendar.anchor
    theirs = build_caption(port.lines, anchor, "{{ drive_url }}").splitlines()
    ours = weekly.texts["weekly_caption"]
    lines = ours.splitlines()
    assert lines[:2] == theirs[:2]  # the title, the week and its days
    change = next(t for t in theirs if t.startswith("Change vs previous week"))
    assert change in lines
    for label, ours_label in [("Net sales", "Net Sales"), ("Orders", "Orders"),
                              ("Avg order value", "Avg Order Value")]:  # fmt: skip
        old = next(t for t in theirs if t.startswith(f"{label}: "))
        new = next(t for t in lines if t.startswith(f"{ours_label}: "))
        value, rest = re.match(r"[^:]+: (?:IDR )?([\d,]+) \((.*)\)", old).groups()
        assert value in new, (old, new)
        if rest == "no prior week" or "(n=" in new:  # under the sample-size rule: no change
            assert "vs" not in new
        elif rest != "unchanged":
            sign = "+" if rest.startswith("▲") else "-"
            assert f"({sign}{rest[2:]} vs prior 7 days)" in new, (old, new)
    last = port.lines["sale_ts"].max()
    assert f"Last sale {last:%d %b %H:%M}" in lines
    assert lines[-1] == theirs[-1] == "PDF and Excel lines: {{ drive_url }}"
    assert len(ours) <= 1024


# --- the category deep dive (9.5) ----------------------------------------------------------------


@pytest.fixture(scope="module")
def deep_dive(port):
    report = load_reports(COMPONENTS)["category_deep_dive"]
    return run_report(report, port.ctx, generated=GENERATED)


def _slot(page, component: str) -> pd.DataFrame:
    (slot,) = [s for s in page.slots if s.slot.spec.component == component]
    (facet,) = slot.facets
    return facet.result.rows


def test_the_deep_dive_has_a_page_for_each_of_the_top_5_categories(port, deep_dive):
    cats = port.sections["L6W"].categories
    sold = cats[cats["net_sales"] > 0].sort_values(
        ["net_sales", "adjusted_category"], ascending=[False, True]
    )
    expected = sold["adjusted_category"].head(5).tolist()
    assert [p.page.title for p in deep_dive.pages] == expected
    assert [p.page.params["category"] for p in deep_dive.pages] == expected


def test_the_deep_dive_top_items_are_the_categorys(port, deep_dive):
    lines = port.lines[port.sections["L6W"].period.mask(port.lines["sale_ts"])]
    for page in deep_dive.pages:
        ours = _slot(page, "top_items")
        mine = lines[lines["adjusted_category"] == page.page.title]
        theirs = (
            mine.groupby("adjusted_item_variant")["net_sales"].sum().reset_index()
            .sort_values(["net_sales", "adjusted_item_variant"], ascending=[False, True])
            .head(8)
        )  # fmt: skip
        assert ours["adjusted_item_variant"].tolist() == theirs["adjusted_item_variant"].tolist()
        assert same(ours["net_sales"], theirs["net_sales"])
        assert same(ours["share"], theirs["net_sales"] / lines["net_sales"].sum())  # of all


def test_the_deep_dive_attach_rate_splits_the_categorys_orders(port, deep_dive):
    lines = port.lines[port.sections["L6W"].period.mask(port.lines["sale_ts"])]
    lines = lines[lines["counts_as_order"]]
    for page in deep_dive.pages:
        rows = _slot(page, "category_penetration")
        receipts = lines[lines["adjusted_category"] == page.page.title]
        flags = receipts.groupby("receipt_nr")[["has_food", "has_drink"]].first()
        theirs = flags.value_counts(normalize=True)
        ours = rows.set_index(["has_food", "has_drink"])["penetration"]
        assert same([ours.sum()], [1.0])
        assert {tuple(bool(v) for v in k) for k in ours.index} == set(theirs.index)
        for key, share in theirs.items():
            assert same([ours.loc[key]], [share]), (page.page.title, key)
