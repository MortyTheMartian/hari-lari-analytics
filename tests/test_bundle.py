"""The JSON results bundle (plan 8.9): every bundle the builder writes passes its schema, the
schema file is current, and the bundle holds what a frontend needs and nothing it should not."""

from __future__ import annotations

import copy
import json
from datetime import date

import jsonschema
import pandas as pd
import pytest
from pydantic import ValidationError

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.analysis.result import Result as ResultModel
from hari_lari_analytics.analysis.result import ResultMeta
from hari_lari_analytics.presentation.builder import run_report
from hari_lari_analytics.presentation.gallery import make_gallery
from hari_lari_analytics.presentation.outputs import bundle_schema
from hari_lari_analytics.presentation.outputs.bundle import bundle, result_json, to_json
from hari_lari_analytics.presentation.visuals.theme import DAYLIGHT, DEFAULT_THEME, TOKENS, Theme
from hari_lari_analytics.warehouse.db import open_run

from .components_fixture import all_components
from .conftest import FIXTURES
from .test_gallery import snapshot_params
from .test_report_build import (
    ANCHOR,
    GENERATED,
    category_pages,
    make,
    tracer_spec,
)

SCHEMA = json.loads(bundle_schema.schema_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def context(run_dir, anchor=ANCHOR):
    db = open_run(run_dir, FIXTURES, anchor)
    return QueryContext.create(db.con, run=db.run, checks=db.checks)


@pytest.fixture(scope="module")
def ctx(run_dir):
    return context(run_dir)


def plain(run) -> dict:
    """The bundle as the file holds it: through JSON text."""
    return json.loads(to_json(run))


def valid(raw: dict) -> None:
    VALIDATOR.validate(raw)
    bundle_schema.check_bundle(raw)


@pytest.fixture(scope="module")
def tracer(ctx):
    return plain(run_report(make(), ctx, GENERATED))


@pytest.fixture(scope="module")
def gallery(ctx):
    report = make_gallery(all_components(), snapshot_params()).report
    return plain(run_report(report, ctx, GENERATED))


# --- the schema file ----------------------------------------------------------------------------


def test_the_schema_file_is_up_to_date():
    path = bundle_schema.schema_path()
    assert path.read_text() == bundle_schema.schema_text(), (
        "run `uv run hari-lari-report schema` and commit the result"
    )


def test_the_schema_is_itself_a_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["title"].startswith("Hari Lari report bundle")


# --- every bundle passes ------------------------------------------------------------------------


def test_the_tracer_bundle_passes_its_schema(tracer):
    valid(tracer)


def test_the_gallery_bundle_passes_its_schema(gallery):
    """Every visual and every shape of the components of the tests: single, rows, series, matrix
    and decomposition results, facets by metric, period and dimension."""
    valid(gallery)
    shapes = {r["shape"] for p in gallery["pages"] for s in p["slots"] for r in s["results"]}
    assert {"single", "rows", "series", "decomposition"} <= shapes


def test_a_repeated_report_passes_its_schema(ctx):
    report = make(pages=category_pages(), outputs={}, text_blocks={}, deliver={})
    raw = plain(run_report(report, ctx, GENERATED))
    valid(raw)
    assert [p["base"] for p in raw["pages"]] == ["cat", "cat"]
    assert [p["id"] for p in raw["pages"]] == ["cat_coffee", "cat_mains"]


def test_an_empty_page_passes_its_schema(run_dir):
    quiet = context(run_dir, date(2026, 11, 24))
    pages = [
        {
            "repeat": {"period": ["L14D"]},
            "when_empty": {"title": "Overview", "message": "No sales in {{ period }}"},
            "pages": tracer_spec()["pages"],
        }
    ]
    raw = plain(
        run_report(make(pages=pages, outputs={}, text_blocks={}, deliver={}), quiet, GENERATED)
    )
    valid(raw)
    (page,) = raw["pages"]
    assert page["empty_message"] == "No sales in L14D"
    assert page["slots"] == []


def test_the_result_of_a_python_source_passes_its_schema():
    """A result without a window (the notes, the lines export) has only columns and rows."""
    df = pd.DataFrame({"kind": ["source", "check"], "note": ["Moka API extraction", None]})
    raw = result_json(ResultModel(df, ResultMeta(shape="rows", window=None, empty=False)))
    assert set(raw) == {"shape", "empty", "columns", "rows"}
    bundle_schema.Result.model_validate(json.loads(json.dumps(raw)))
    assert raw["rows"][1]["cells"]["note"] == {"value": None, "text": ""}


def test_a_report_with_insights_passes_its_schema(ctx):
    raw = plain(run_report(make(), ctx, GENERATED))
    valid(raw)
    assert "insights" in raw["pages"][0]


# --- the contract is strict ---------------------------------------------------------------------


def broken(raw: dict, edit) -> dict:
    copied = copy.deepcopy(raw)
    edit(copied)
    return copied


@pytest.mark.parametrize(
    "edit",
    [
        lambda b: b.update(format_version=2),
        lambda b: b.update(extra=1),
        lambda b: b.pop("theme"),
        lambda b: b["report"].pop("anchor"),
        lambda b: b["pages"][0].update(chips=[{"text": "x", "style": "loud"}]),
        lambda b: b["pages"][0]["slots"][0]["results"][0].update(shape="cube"),
        lambda b: b["pages"][0]["slots"][0]["results"][0]["rows"][0]["cells"].update(
            net_sales={"value": 1, "tone": "great", "text": "1"}
        ),
        lambda b: b["pages"][0]["slots"][0]["results"][0]["rows"][0]["cells"].update(
            net_sales={"value": 1}
        ),
        lambda b: b["pages"][0]["slots"][0].update(sql="select 1"),
        lambda b: b.update(pages=[]),
        lambda b: b["theme"]["colors"].update(sparkle="#fff"),
    ],
    ids=[
        "version",
        "unknown key",
        "no theme",
        "no anchor",
        "chip style",
        "shape",
        "tone",
        "cell without text",
        "a key the contract lacks",
        "no page",
        "unknown token",
    ],
)
def test_a_change_to_the_contract_fails_the_schema(tracer, edit):
    raw = broken(tracer, edit)
    assert list(VALIDATOR.iter_errors(raw))
    with pytest.raises(ValidationError):
        bundle_schema.check_bundle(raw)


# --- what a frontend needs ----------------------------------------------------------------------


def slots(raw: dict):
    return [(p, s) for p in raw["pages"] for s in p["slots"]]


def test_the_theme_gives_every_token_in_the_colour_of_the_pdf(tracer):
    colors = tracer["theme"]["colors"]
    assert set(colors) == set(TOKENS)
    assert colors["accent"] == DAYLIGHT.primary and colors["bad"] == DAYLIGHT.negative
    assert tracer["theme"]["series"] == list(DAYLIGHT.series)
    assert tracer["theme"]["font"] == "Inter"


def test_a_custom_theme_travels_with_the_bundle(ctx):
    theme = Theme(holiday="#123456")
    raw = json.loads(json.dumps(bundle(run_report(make(), ctx, GENERATED), theme), default=str))
    assert raw["theme"]["colors"]["holiday"] == "#123456"
    assert DEFAULT_THEME.holiday != "#123456"


def test_the_report_names_its_data(tracer):
    report = tracer["report"]
    assert report["anchor"] == "2026-10-13" and report["run"] == "2026-10-14"
    assert report["generated"] == "2026-10-14T07:00:00"
    assert report["title"] == "Tracer report" and report["footer"].startswith("Test café")


def test_every_facet_points_at_a_result_of_its_slot(gallery):
    for page, slot in slots(gallery):
        for facet in slot["facets"]:
            assert 0 <= facet["result"] < len(slot["results"]), (page["id"], slot["component"])
        used = {f["result"] for f in slot["facets"]}
        assert used == set(range(len(slot["results"])))  # no result nobody shows


def test_every_cell_has_a_column_and_a_display_string(gallery):
    for _, slot in slots(gallery):
        for result in slot["results"]:
            ids = {c["id"] for c in result["columns"]}
            assert len(ids) == len(result["columns"])
            for row in result["rows"]:
                assert set(row["cells"]) <= ids
                assert all(isinstance(c["text"], str) for c in row["cells"].values())


def test_values_that_are_money_carry_their_unit_and_format(tracer):
    table = tracer["pages"][0]["slots"][1]["results"][0]
    net_sales = next(c for c in table["columns"] if c["id"] == "net_sales")
    assert (net_sales["kind"], net_sales["format"], net_sales["unit"]) == ("value", "idr", "IDR")
    assert table["metrics"]["net_sales"]["label"] == "Net Sales"
    total = next(r for r in table["rows"] if r["kind"] == "total")
    assert total["cells"]["net_sales"]["text"] == "213,000"
    assert total["cells"]["net_sales"]["value"] == 213000


def test_a_comparison_that_is_not_valid_says_so(tracer):
    window = tracer["pages"][0]["slots"][1]["results"][0]["window"]
    assert window["compare_ok"] is False and window["comparison"] is not None
    row = tracer["pages"][0]["slots"][1]["results"][0]["rows"][0]
    assert row["cells"]["net_sales_change"]["text"] == "n/a"


def test_a_small_sample_is_flagged_for_the_renderer(tracer):
    table = tracer["pages"][0]["slots"][1]["results"][0]
    assert all(r.get("small") for r in table["rows"])  # 4 orders: below the default rule
    assert all(r["n"] <= 4 for r in table["rows"])


def test_the_window_of_the_page_and_the_chips_are_there(tracer):
    page = tracer["pages"][0]
    assert page["period"] == "L14D"
    assert [c["text"] for c in page["chips"]][:2] == ["Last 14 days", "30 Sep 2026 – 13 Oct 2026"]
    assert page["grid"] == {"columns": 12, "rows": [1, 4]}


def test_the_text_blocks_keep_the_drive_placeholder(tracer):
    assert tracer["texts"]["summary"].endswith("{{ drive_url }}")


# --- what must not be in it ---------------------------------------------------------------------


def test_no_free_text_of_the_lines_is_in_the_bundle(gallery, tracer):
    for text in (json.dumps(gallery), json.dumps(tracer)):
        assert "oat milk please" not in text  # the note of a fixture line
        assert "receipt_nr" not in text and "customer" not in text.lower()


def test_no_sql_is_in_the_bundle(gallery):
    text = json.dumps(gallery).lower()
    assert "select " not in text and " from " not in text


def test_the_bundle_is_plain_json(tracer):
    text = to_json_text(tracer)
    assert "NaN" not in text and "Infinity" not in text


def to_json_text(raw: dict) -> str:
    return json.dumps(raw, allow_nan=False)
