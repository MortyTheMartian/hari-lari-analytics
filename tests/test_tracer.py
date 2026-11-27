"""The tracer check, the exit of phase 7 (plan 7.15): two packaged components run through the
whole core (loader, parameters, planned query, compiler, transforms, facets, and the CLI) and
equal the old pandas pipeline, frozen in `tests/golden/` on the fixture: the full-history KPIs
of the cover, and `category_contributive_aov` in L14D, L6W and L3M. The CLI prints the numbers
of the facets on the fixture and, when it is present, on the real run of 2026-11-22."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.core.store import LocalStore
from hari_lari_analytics.orchestration import cli
from hari_lari_analytics.presentation.components import load_components
from hari_lari_analytics.presentation.facets import run_facets
from hari_lari_analytics.warehouse.build import build_warehouse
from hari_lari_analytics.warehouse.db import open_run

from . import golden
from .conftest import FIXTURES
from .test_models import LAST_SALE, REAL_RUN

REAL_ANCHOR = date(2026, 11, 22)
PERIODS = ["L14D", "L6W", "L3M"]
real = pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run")


def _ctx(run: Path, config: Path, anchor: date) -> QueryContext:
    db = open_run(run, config, anchor)
    return QueryContext.create(db.con, run=db.run, checks=db.checks)


@pytest.fixture(scope="module")
def ctx(run_dir):
    return _ctx(run_dir, FIXTURES, LAST_SALE)


@pytest.fixture(scope="module")
def components():
    return load_components()


@pytest.fixture(scope="module")
def metrics():
    return golden.numbers("metrics").set_index("scope")


def same(a, b) -> bool:
    return np.allclose(np.asarray(a, float), np.asarray(b, float), equal_nan=True, rtol=1e-12)


def test_the_fixture_run_has_the_golden_lines(ctx):
    golden.assert_lines(ctx.con.execute("select * from fct_sales_lines").df())


def test_full_history_kpis(ctx, components, metrics):
    facets = run_facets(components["full_history_kpis"], ctx)
    expected = metrics.loc["full"]  # the cover of the weekly report
    assert [f.metric for f in facets] == ["net_sales", "orders", "aov", "items_sold"]
    for facet in facets:
        assert same([facet.result.df.iloc[0][facet.metric]], [expected[facet.metric]]), facet.key
    window = facets[0].result.meta.window.current
    assert (window.start, window.end) == (expected.first_sale_ts.date(), LAST_SALE)


def _category_net_sales(period: str) -> pd.Series:
    cats = golden.numbers("metrics_by_category")
    cats = cats[cats["scope"] == period]
    return cats.set_index("adjusted_category")["net_sales"].sort_index()


@pytest.mark.parametrize("period", PERIODS)
def test_category_contributive_aov(ctx, components, metrics, period):
    component = components["category_contributive_aov"]
    [facet] = run_facets(component, ctx, {"period": period})
    r = facet.result
    assert r.meta.compare_ok == metrics.loc[period, "compare_ok"]
    assert r.meta.trading_days == metrics.loc[period, "trading_days"]
    expected = _category_net_sales(period)
    assert same([r.total["net_sales"]], [metrics.loc[period, "net_sales"]])
    if r.meta.empty:
        assert expected.empty
        return
    ours = r.rows.set_index("adjusted_category")["net_sales"].sort_index()
    assert set(expected.index) <= set(ours.index)  # and the categories sold only before
    assert same(ours, expected.reindex(ours.index, fill_value=0))
    assert same(r.rows["contributive_aov"].sum(), r.total["contributive_aov"])
    hint = cli.render_text(component.raw["hint"], {"period": period}, r, facet)
    assert hint.endswith("Δ vs prior period") == metrics.loc[period, "compare_ok"]


REAL_LANDING = Path("data/landing")
real_landed = pytest.mark.skipif(
    not (REAL_LANDING / "moka" / "2026-11-22").is_dir() or not REAL_RUN.is_dir(),
    reason="needs the real extraction run, landed and as it was extracted",
)


@pytest.mark.parametrize("which", ["fixture", pytest.param("real", marks=real_landed)])
def test_the_cli_prints_the_numbers_of_the_facets(
    which, run_dir, warehouse, components, capsys, tmp_path
):
    if which == "fixture":
        run, config, anchor, stored = run_dir, FIXTURES, LAST_SALE, warehouse
    else:
        run, config, anchor = REAL_RUN, Path("config"), REAL_ANCHOR
        root = LocalStore(tmp_path)
        info = build_warehouse(root, REAL_ANCHOR, "config", landing=LocalStore(REAL_LANDING))
        stored = root.root / info.database
    component = components["category_contributive_aov"]
    [facet] = run_facets(component, _ctx(run, config, anchor), {"period": "L6W"})
    expected = facet.result
    args = ["run", "category_contributive_aov", "--period", "L6W", "--csv",
            "--warehouse", str(stored), "--anchor", anchor.isoformat()]  # fmt: skip
    assert cli.main(args) == 0
    df = pd.read_csv(io.StringIO(capsys.readouterr().out))
    rows = df[~df["is_total"]]
    assert rows["adjusted_category"].tolist() == expected.rows["adjusted_category"].tolist()
    assert same(rows["contributive_aov"], expected.rows["contributive_aov"])
    assert same(df[df["is_total"]]["contributive_aov"], [expected.total["contributive_aov"]])
    assert same(rows["net_sales"].sum(), expected.total["net_sales"])
