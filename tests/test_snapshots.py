"""Every component, packaged and of the test fixtures, against its golden result on the test
fixture run (plan 8.7).

`tests/snapshots/<id>.yml` names the parameters and holds the result of each facet. A change to
the compiler, a transform, a metric or a component shows here as a diff; after checking it,
refresh with `uv run pytest tests/test_snapshots.py --update-snapshots`. The gallery
(`tests/test_gallery.py`) draws the same components with the same parameters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.presentation.facets import run_facets
from hari_lari_analytics.presentation.snapshots import read_snapshot, snapshot_text
from hari_lari_analytics.warehouse.db import connect

from .api_fixture import EXTRACTED_AT, api_tables
from .components_fixture import all_components
from .test_models import LAST_SALE, SEEDS

SNAPSHOTS = Path(__file__).parent / "snapshots"
COMPONENTS = sorted(all_components())


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE, extracted_at=EXTRACTED_AT))


@pytest.fixture(scope="module")
def components():
    return all_components()


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
