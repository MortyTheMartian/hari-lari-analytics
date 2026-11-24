"""Every packaged component against its golden result on the test fixture.

`tests/snapshots/<id>.yml` names the parameters and holds the result of each facet. A change to
the compiler, a transform, a metric or a component shows here as a diff; after checking it,
refresh with `uv run pytest tests/test_snapshots.py --update-snapshots`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hari_lari_analytics.reporting.components import components_dir, load_components
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.snapshots import read_snapshot, snapshot_text

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

SNAPSHOTS = Path(__file__).parent / "snapshots"
COMPONENTS = sorted(p.stem for p in components_dir().glob("*.yml"))


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


@pytest.fixture(scope="module")
def components():
    return load_components()


@pytest.mark.parametrize("component_id", COMPONENTS)
def test_component_snapshot(component_id, ctx, components, request):
    path = SNAPSHOTS / f"{component_id}.yml"
    update = request.config.getoption("--update-snapshots")
    saved = read_snapshot(path.read_text()) if path.exists() else {}
    params = saved.get("params") or {}
    facets = run_facets(components[component_id], ctx, params)
    text = snapshot_text(component_id, params, facets)
    if update:
        path.write_text(text)
        return
    assert "facets" in saved, (
        f"no snapshot of {component_id} yet: run "
        f"`uv run pytest tests/test_snapshots.py --update-snapshots -k {component_id}`"
    )
    assert text == path.read_text()


def test_every_snapshot_has_a_component():
    orphans = sorted({p.stem for p in SNAPSHOTS.glob("*.yml")} - set(COMPONENTS))
    assert not orphans, f"snapshots without a component: {orphans}"
