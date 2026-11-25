"""The tracer check, the exit of phase 7 (plan 7.15): two packaged components run through the
whole core (loader, parameters, planned query, compiler, transforms, facets, and the CLI) on
the real run of 2026-11-22, and equal the old pandas pipeline on the same run: the full-history
KPIs of the cover, and `category_contributive_aov` in L14D, L6W and L3M."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.dimensions import prepare_lines
from hari_lari_analytics.load import load_aliases
from hari_lari_analytics.load_api import load_api_sales
from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.reporting import cli
from hari_lari_analytics.reporting.components import load_components
from hari_lari_analytics.reporting.db import open_run
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext

from .test_models import REAL_RUN

ANCHOR = date(2026, 11, 22)
PERIODS = ["L14D", "L6W", "L3M"]
pytestmark = pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run")


@pytest.fixture(scope="module")
def lines():
    """The old pandas lines of the run."""
    _, result = load_api_sales(REAL_RUN, load_aliases(Path("config/item_aliases.csv")))
    lines, _ = prepare_lines(result, Path("config/category_group.csv"), ANCHOR)
    return lines


@pytest.fixture(scope="module")
def ctx():
    db = open_run(REAL_RUN, Path("config"), ANCHOR)
    return QueryContext.create(db.con, run=db.run, checks=db.checks)


@pytest.fixture(scope="module")
def components():
    return load_components()


def same(a, b) -> bool:
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=1e-12)


def test_full_history_kpis(lines, ctx, components):
    facets = run_facets(components["full_history_kpis"], ctx)
    expected = compute_metrics(lines).iloc[0]  # the cover of the weekly report
    assert [f.metric for f in facets] == ["net_sales", "orders", "aov", "items_sold"]
    for facet in facets:
        assert same([facet.result.df.iloc[0][facet.metric]], [expected[facet.metric]]), facet.key
    window = facets[0].result.meta.window.current
    assert (window.start, window.end) == (lines["sale_ts"].min().date(), ANCHOR)


def _period_net_sales(lines: pd.DataFrame, key: str) -> pd.Series:
    period = next(p for p in report_periods(ANCHOR) if p.key == key)
    in_period = lines[period.mask(lines["sale_ts"])]
    return in_period.groupby("adjusted_category")["net_sales"].sum().sort_index()


@pytest.mark.parametrize("period", PERIODS)
def test_category_contributive_aov(lines, ctx, components, period):
    p = next(p for p in report_periods(ANCHOR) if p.key == period)
    orders = lines[lines["counts_as_order"] & p.mask(lines["sale_ts"])]
    component = components["category_contributive_aov"]
    [facet] = run_facets(component, ctx, {"period": period})
    r = facet.result
    prior_ok = p.prior_start >= lines["sale_ts"].min().date()
    assert r.meta.compare_ok == prior_ok
    assert r.meta.trading_days == orders["sale_date"].nunique()
    expected = _period_net_sales(lines, period)
    ours = r.rows.set_index("adjusted_category")["net_sales"].sort_index()
    assert set(expected.index) <= set(ours.index)  # and the categories sold only before
    assert same(ours, expected.reindex(ours.index, fill_value=0))
    assert same([r.total["net_sales"]], [expected.sum()])
    assert same(r.rows["contributive_aov"].sum(), r.total["contributive_aov"])
    hint = cli.render_text(component.raw["hint"], {"period": period}, r, facet)
    assert hint.endswith("Δ vs prior period") == prior_ok


def test_the_cli_prints_the_same_numbers(lines, ctx, components, capsys):
    [facet] = run_facets(components["category_contributive_aov"], ctx, {"period": "L6W"})
    expected = facet.result
    args = ["run", "category_contributive_aov", "--period", "L6W", "--csv",
            "--run", str(REAL_RUN), "--anchor", ANCHOR.isoformat()]  # fmt: skip
    assert cli.main(args) == 0
    df = pd.read_csv(io.StringIO(capsys.readouterr().out))
    rows = df[~df["is_total"]]
    assert rows["adjusted_category"].tolist() == expected.rows["adjusted_category"].tolist()
    assert same(rows["contributive_aov"], expected.rows["contributive_aov"])
    assert same(df[df["is_total"]]["contributive_aov"], [expected.total["contributive_aov"]])
    assert same(rows["net_sales"].sum(), _period_net_sales(lines, "L6W").sum())
