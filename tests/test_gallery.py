"""The gallery (plan 8.7): every component on the fixture run, one page each, without text
overflow, with the parameters of its snapshot."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from hari_lari_analytics.reporting import report_cli
from hari_lari_analytics.reporting.components import load_components
from hari_lari_analytics.reporting.db import open_run
from hari_lari_analytics.reporting.gallery import (
    build_gallery,
    gallery_spec,
    make_gallery,
    slot_height,
)
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.reports import ReportError
from hari_lari_analytics.reporting.snapshots import read_snapshot

from .components_fixture import FIXTURE_COMPONENTS, all_components
from .conftest import FIXTURES
from .test_report_build import ANCHOR, GENERATED
from .test_snapshots import SNAPSHOTS

NO_VISUAL = {"lines_export", "net_sales_drivers", "aov_drivers"}  # the last two: insights only


def snapshot_params() -> dict[str, dict]:
    return {
        p.stem: read_snapshot(p.read_text()).get("params") or {} for p in SNAPSHOTS.glob("*.yml")
    }


@pytest.fixture(scope="module")
def ctx(run_dir):
    db = open_run(run_dir, FIXTURES, ANCHOR)
    return QueryContext.create(db.con, run=db.run, checks=db.checks)


@pytest.fixture(scope="module")
def built(ctx, tmp_path_factory):
    out = tmp_path_factory.mktemp("gallery")
    result, skipped = build_gallery(
        all_components(), ctx, out, snapshot_params(), generated=GENERATED
    )
    return result, skipped


def test_every_component_with_a_visual_has_a_page(built):
    result, skipped = built
    pages = [p.page.id for p in result.run.pages]
    expected = sorted(set(all_components()) - NO_VISUAL)
    assert pages == expected
    assert {s.component: s.reason for s in skipped} == dict.fromkeys(NO_VISUAL, "no visual")


def test_no_text_overflows_its_slot(built):
    result, _ = built
    assert [o.describe() for o in result.overflows] == []


def test_writes_a_pdf_and_a_png_per_page(built):
    result, _ = built
    assert result.pdf and result.pdf.exists()
    assert set(result.pngs) == {p.page.id for p in result.run.pages}
    for path in result.pngs.values():
        with Image.open(path) as image:
            assert image.size[0] > image.size[1]  # landscape
    assert result.xlsx is None and not result.texts


def test_the_bundle_lists_every_page(built):
    result, _ = built
    bundle = json.loads(result.bundle.read_text())
    assert [p["id"] for p in bundle["pages"]] == [p.page.id for p in result.run.pages]


def test_the_slot_is_at_least_the_preview_height():
    comps = all_components()
    spec, _ = gallery_spec(comps, snapshot_params())
    heights = {p["id"]: p["slots"][0]["height"] for p in spec["pages"]}
    assert all(3 <= h <= 6 for h in heights.values())


def test_parameters_without_a_default_skip_the_component():
    from .test_visuals import component

    comp = component(
        """
query: {model: fct_sales_lines, dimensions: [adjusted_category], metrics: [net_sales],
        window: L14D, where: {adjusted_category: $category}}
visual: {type: table}
""",
        "needs_category",
    )
    spec, skipped = gallery_spec({"needs_category": comp})
    assert spec["pages"] == []
    assert [(s.component, "category" in s.reason) for s in skipped] == [("needs_category", True)]
    with pytest.raises(ReportError, match="no page"):
        make_gallery({"needs_category": comp})
    given, _ = gallery_spec({"needs_category": comp}, {"needs_category": {"category": "Coffee"}})
    assert given["pages"][0]["slots"][0]["params"] == {"category": "Coffee"}


def test_a_component_that_never_fits_names_itself(monkeypatch):
    from hari_lari_analytics.reporting import gallery

    comp = load_components(FIXTURE_COMPONENTS)["kpi_cards"]
    monkeypatch.setattr(gallery.layouts, "check_size", _always_too_small)
    with pytest.raises(ReportError, match="kpi_cards.yml.*whole page"):
        slot_height(comp, {})


def _always_too_small(*args, **kwargs):
    raise gallery_error("too small")


def gallery_error(message):
    from hari_lari_analytics.reporting.layouts import LayoutError

    return LayoutError(message)


def test_cli_builds_the_gallery(run_dir, tmp_path, capsys):
    code = report_cli.main(
        [
            "--components",
            str(FIXTURE_COMPONENTS),
            "gallery",
            "--run",
            str(run_dir),
            "--config",
            str(FIXTURES),
            "--anchor",
            ANCHOR.isoformat(),
            "--out",
            str(tmp_path),
            "--no-png",
            "--strict",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    files = {line.rsplit("/", 1)[-1] for line in captured.out.splitlines()}
    assert files == {"gallery_2026-10-13.pdf", "bundle.json"}
    assert "Run 2026-10-14" in captured.err
