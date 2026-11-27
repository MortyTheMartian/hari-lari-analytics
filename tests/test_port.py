"""The port of the weekly report (plan 9.3): the components of the cover give the numbers of
the old pandas pipeline, and `reports/weekly.yml` (9.4) has the pages, chips and footer of the
PDF before the port. The category deep dive (9.5) is checked against the lines.

The page-by-page numbers were compared with the old report (`report_data.py`) until it was
deleted at the cut-over (9.7b).

The drawing may differ, so only the results are compared. Each test runs on the fixture run and,
when it is present, on the real run of 2026-11-22; on the fixture, L14D has no comparison and
L3M has no sales, so the empty and the comparison-less cases are covered too. The fixture's
lines, periods and cover numbers are the golden ones of the pandas pipeline (`tests/golden/`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pytest
from matplotlib.text import Text

from hari_lari_analytics.load_api import ApiRun
from hari_lari_analytics.reporting.builder import run_report
from hari_lari_analytics.reporting.checks import check
from hari_lari_analytics.reporting.components import load_components
from hari_lari_analytics.reporting.db import connect, open_run
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.outputs.pages import render_page
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.reports import load_reports
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME

from . import golden
from .api_fixture import api_tables
from .test_models import LAST_SALE, REAL_RUN, SEEDS

COMPONENTS = load_components()
PERIODS = ["L14D", "L6W", "L3M"]
REAL_ANCHOR = date(2026, 11, 22)
real = pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run")


@dataclass
class Port:
    ctx: QueryContext
    lines: pd.DataFrame
    periods: dict[str, golden.Period]

    @property
    def first(self) -> date:
        return self.lines["sale_ts"].min().date()

    def prior_ok(self, key: str) -> bool:
        return self.periods[key].prior_start >= self.first

    def empty(self, key: str) -> bool:
        return not self.periods[key].mask(self.lines["sale_ts"]).any()

    def in_period(self, key: str) -> pd.DataFrame:
        return self.lines[self.periods[key].mask(self.lines["sale_ts"])]

    def run(self, component: str, period: str | None = None) -> list:
        params = {"period": period} if period else None
        return run_facets(COMPONENTS[component], self.ctx, params)

    def result(self, component: str, period: str | None = None) -> Result:
        (facet,) = self.run(component, period)
        return facet.result


def _port(con, run: ApiRun, anchor: date) -> Port:
    lines = con.execute("select * from fct_sales_lines").df()
    return Port(QueryContext.create(con, run=run, checks=check(con)), lines, golden.periods(anchor))


@pytest.fixture(scope="module")
def fixture_port() -> Port:
    run = ApiRun("data/extract/2026-10-14", {}, {"warnings": ["last sale is old"]})
    port = _port(connect(api_tables(), SEEDS, LAST_SALE), run, LAST_SALE)
    golden.assert_lines(port.lines)
    return port


@pytest.fixture(scope="module", params=["fixture", pytest.param("real", marks=real)])
def port(request, fixture_port) -> Port:
    if request.param == "fixture":
        return fixture_port
    db = open_run(REAL_RUN, Path("config"), REAL_ANCHOR)
    return _port(db.con, db.run, REAL_ANCHOR)


def same(a, b) -> bool:
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=1e-12)


def values(column: pd.Series) -> list:
    return [None if pd.isna(v) else v for v in column]


# --- the cover ----------------------------------------------------------------------------------


def test_full_history_cards_amounts_and_last_sale(fixture_port):
    port = fixture_port
    t = golden.numbers("metrics").set_index("scope").loc["full"]
    cards = port.run("full_history_kpis")
    assert [f.metric for f in cards] == ["net_sales", "orders", "aov", "items_sold"]
    row = cards[0].result.df.iloc[0]
    assert same([row[m] for m in ["net_sales", "orders", "aov", "items_sold"]],
                [t.net_sales, t.orders, t.aov, t.items_sold])  # fmt: skip
    amounts = port.result("full_history_amounts").df.iloc[0]
    assert same([amounts.gross_sales, amounts.discounts, amounts.refunds],
                [t.gross_sales, t.discounts, t.refunds])  # fmt: skip
    assert port.result("last_sale").df.iloc[0].last_sale_ts == t.last_sale_ts


def test_period_cards(port):
    facets = port.run("period_summaries")
    assert [f.key for f in facets] == PERIODS
    for facet in facets:
        period, r = port.periods[facet.key], facet.result
        assert facet.label == period.title and r.meta.window.current.label == period.label
        assert r.meta.compare_ok == port.prior_ok(facet.key)
        assert r.meta.empty == port.empty(facet.key)
        assert same([r.total["net_sales"]], [port.in_period(facet.key)["net_sales"].sum()])


def test_the_data_notes_from_the_run_manifest(port):
    notes = port.result("data_notes").df
    run = port.ctx.run
    assert notes.iloc[0].tolist() == ["source", f"Moka API extraction {run.name}"]
    expected = [f"Extraction warning: {w}" for w in run.manifest.get("warnings", [])]
    assert notes[notes["kind"] == "extraction"]["note"].tolist() == expected
    assert notes[notes["kind"] == "check"]["note"].tolist() == [
        w.describe() for w in port.ctx.checks.warnings
    ]


# --- the workbook -------------------------------------------------------------------------------


def test_the_lines_export_has_the_lines_of_the_excel_file(port):
    from hari_lari_analytics.report_excel import COLUMNS

    df = port.result("lines_export").df
    assert list(df.columns) == [c for c, *_ in COLUMNS]
    columns = list(df.columns)
    pd.testing.assert_frame_equal(golden.as_text(df), golden.as_text(port.lines, columns))


# --- the weekly report (9.4): pages and chrome ----------------------------------------------------


@pytest.fixture(scope="module")
def weekly(port):
    report = load_reports(COMPONENTS)["weekly"]
    return run_report(report, port.ctx, generated=GENERATED)


GENERATED = datetime(2026, 11, 25, 15, 0)


def _today_pages(port: Port) -> list[tuple[str, list[tuple[str, str]], str]]:
    """The title, chips and note of each page of the PDF before the port."""
    first, anchor = port.first, port.ctx.calendar.anchor
    pages = [("Sales report", [("Full history", "fill"),
                               (f"{first:%d %b %Y} – {anchor:%d %b %Y}", "line")], "")]  # fmt: skip
    for key in PERIODS:
        period = port.periods[key]
        chips = [(period.title, "fill"), (period.label, "line")]
        if port.prior_ok(key):
            chips.append((f"vs {period.prior_label}", "muted"))
            note = ""
        else:
            chips.append(("no comparison", "muted"))
            note = f"The prior period starts before the first sale ({first:%d %b %Y})."
        titles = (
            ["Overview"]
            if port.empty(key)
            else ["Overview", "Categories", "Items, hours & payments"]
        )
        pages += [(title, chips, note) for title in titles]
    return pages


def test_the_weekly_pages_are_todays_in_todays_order_with_todays_chips(port, weekly):
    ours = [(p.page.title, p.chips, p.note) for p in weekly.pages]
    assert ours == _today_pages(port)


def test_an_empty_period_is_one_page_with_todays_message(port, weekly):
    empty = [p.page for p in weekly.pages if p.page.empty_message]
    expected = [k for k in PERIODS if port.empty(k)]
    assert [p.period for p in empty] == expected
    for page in empty:
        assert (page.title, page.empty_message) == ("Overview", "No sales in this period")


def test_the_footer_is_todays(port, weekly):
    anchor = port.ctx.calendar.anchor
    assert weekly.footer == (
        f"Hari Lari Cafe · Moka POS sales · data through {anchor:%d %b %Y} · "
        f"generated {GENERATED:%d %b %Y %H:%M}"
    )


def test_the_weekly_pages_fit_with_their_sentences_and_every_table_shows_all_its_rows(port, weekly):
    """Plan 9.8: no text over its slot on any page of L14D, L6W and L3M, and no table that ends
    in "… and n more rows" (the sentences and the tags take room from the charts)."""
    with matplotlib.rc_context(DEFAULT_THEME.rc()):
        for page in weekly.pages:
            fig, overflows = render_page(weekly, page, DEFAULT_THEME)
            where = f"{page.page.id}"
            assert [o.describe() for o in overflows] == [], where
            texts = [t.get_text() for t in fig.findobj(Text)]
            assert not [t for t in texts if t.startswith("… and ")], where


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
    cats = port.in_period("L6W").groupby("adjusted_category")["net_sales"].sum().reset_index()
    sold = cats[cats["net_sales"] > 0].sort_values(
        ["net_sales", "adjusted_category"], ascending=[False, True]
    )
    expected = sold["adjusted_category"].head(5).tolist()
    assert [p.page.title for p in deep_dive.pages] == expected
    assert [p.page.params["category"] for p in deep_dive.pages] == expected
    assert [p.chips[-1] for p in deep_dive.pages] == [
        (f"Category: {c} (focus)", "line") for c in expected
    ]


def test_the_deep_dive_top_items_are_the_categorys(port, deep_dive):
    lines = port.in_period("L6W")
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
    lines = port.in_period("L6W")
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
