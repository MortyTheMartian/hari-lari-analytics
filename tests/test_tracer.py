"""The tracer check, the exit of phase 7 (plan 7.15): two packaged components run through the
whole core (loader, parameters, planned query, compiler, transforms, facets, and the CLI) on
the real run of 2026-11-22, and equal today's report, built by the old pandas pipeline from the
same run: the full-history KPIs of the cover, and `category_contributive_aov` against
`report_data.Section` in L14D, L6W and L3M."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.cli import prepare_lines
from hari_lari_analytics.load import load_aliases
from hari_lari_analytics.load_api import load_api_sales
from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.report_data import build_section
from hari_lari_analytics.reporting import cli
from hari_lari_analytics.reporting.components import load_components
from hari_lari_analytics.reporting.db import open_run
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext

from .test_models import REAL_RUN

ANCHOR = date(2026, 11, 22)
PERIODS = ["L14D", "L6W", "L3M"]
CATEGORY_COLUMNS = [
    "net_sales", "items_sold", "orders", "share", "penetration", "category_aov",
    "contributive_aov", "prior_penetration", "prior_category_aov", "prior_contributive_aov",
    "category_aov_change", "contributive_aov_delta",
]  # fmt: skip
TOTAL_COLUMNS = [c for c in CATEGORY_COLUMNS if c != "contributive_aov_delta"]

pytestmark = pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run")


@pytest.fixture(scope="module")
def today():
    """Today's report: the old pandas lines of the run, and its sections."""
    _, result = load_api_sales(REAL_RUN, load_aliases(Path("config/item_aliases.csv")))
    lines, _ = prepare_lines(result, Path("config/category_group.csv"), ANCHOR, source="api")
    first = lines["sale_ts"].min().date()
    sections = {p.key: build_section(lines, p, first) for p in report_periods(ANCHOR)}
    return lines, sections


@pytest.fixture(scope="module")
def ctx():
    db = open_run(REAL_RUN, Path("config"), ANCHOR)
    return QueryContext.create(db.con, run=db.run, checks=db.checks)


@pytest.fixture(scope="module")
def components():
    return load_components()


def same(a, b) -> bool:
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=1e-12)


def test_full_history_kpis(today, ctx, components):
    lines, _ = today
    facets = run_facets(components["full_history_kpis"], ctx)
    expected = compute_metrics(lines).iloc[0]  # the cover of report_pdf
    assert [f.metric for f in facets] == ["net_sales", "orders", "aov", "items_sold"]
    for facet in facets:
        assert same([facet.result.df.iloc[0][facet.metric]], [expected[facet.metric]]), facet.key
    window = facets[0].result.meta.window.current
    assert (window.start, window.end) == (lines["sale_ts"].min().date(), ANCHOR)


@pytest.mark.parametrize("period", PERIODS)
def test_category_contributive_aov(today, ctx, components, period):
    s = today[1][period]
    component = components["category_contributive_aov"]
    [facet] = run_facets(component, ctx, {"period": period})
    r = facet.result
    assert r.meta.compare_ok == s.prior_ok and r.meta.trading_days == s.trading_days
    rows, cats = r.rows, s.categories
    assert rows["adjusted_category"].tolist() == cats["adjusted_category"].tolist()
    for c in CATEGORY_COLUMNS:
        assert same(rows[c], cats[c]), c
    assert same(rows["penetration_delta"] * 100, cats["delta_pp"])
    for c in TOTAL_COLUMNS:
        assert same([r.total[c]], [s.category_total[c]]), c
    hint = cli.render_text(component.raw["hint"], {"period": period}, r, facet)
    assert hint.endswith("Δ vs prior period") == s.prior_ok


def test_the_cli_prints_the_same_numbers(today, capsys):
    s = today[1]["L6W"]
    args = ["run", "category_contributive_aov", "--period", "L6W", "--csv",
            "--run", str(REAL_RUN), "--anchor", ANCHOR.isoformat()]  # fmt: skip
    assert cli.main(args) == 0
    df = pd.read_csv(io.StringIO(capsys.readouterr().out))
    rows = df[~df["is_total"]]
    assert rows["adjusted_category"].tolist() == s.categories["adjusted_category"].tolist()
    assert same(rows["contributive_aov"], s.categories["contributive_aov"])
    assert same(df[df["is_total"]]["contributive_aov"], [s.category_total["contributive_aov"]])
