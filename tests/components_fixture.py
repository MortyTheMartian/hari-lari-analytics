"""All the components the tests know: the packaged ones and those of `tests/fixtures/components/`
(which exist to cover what the packaged ones do not). Snapshots and the gallery use both."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from hari_lari_analytics.presentation.components import Component, load_components

FIXTURE_COMPONENTS = Path(__file__).parent / "fixtures" / "components"


@cache
def all_components() -> dict[str, Component]:
    packaged = load_components()
    fixtures = load_components(FIXTURE_COMPONENTS)
    clash = sorted(set(packaged) & set(fixtures))
    assert not clash, f"a fixture component has the id of a packaged one: {clash}"
    return {**packaged, **fixtures}
