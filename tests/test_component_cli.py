"""The developer CLI `hari-lari-component` (plan 7.14), on the fixture run and components."""

from __future__ import annotations

import io
import os
import shutil

import pandas as pd
import pytest
import yaml
from PIL import Image

from hari_lari_analytics.reporting import cli, components, preview
from hari_lari_analytics.reporting.components import SCHEMA_REF, load_component, schema_text
from hari_lari_analytics.reporting.db import open_run
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.snapshots import read_snapshot

from .api_fixture import api_tables
from .conftest import FIXTURES

COMPONENTS = FIXTURES / "components"


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    run = tmp_path_factory.mktemp("extract") / "2026-10-14"
    (run / "tables").mkdir(parents=True)
    for name, table in api_tables().items():
        table.to_parquet(run / "tables" / f"{name}.parquet", index=False)
    return run


def component_cli(capsys, *args, components=COMPONENTS):
    code = cli.main(["--components", str(components), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run(capsys, run_dir, *args):
    return component_cli(capsys, "run", *args, "--run", str(run_dir), "--config", str(FIXTURES))


# --- run ------------------------------------------------------------------------------------------


def test_run_prints_the_result(capsys, run_dir):
    code, out, err = run(capsys, run_dir, "aov_bridge", "--period", "WTD")
    assert code == 0, err
    assert "Run 2026-10-14, data through 13 Oct 2026" in err
    assert out.startswith("== Why the average order value moved\n")
    assert "WTD: 12 Oct 2026 – 13 Oct 2026 · vs same days last week · shape decomposition" in out
    assert "items_per_order" in out and "with\n" not in out  # no SQL without --sql


def test_run_with_sql(capsys, run_dir):
    code, out, _ = run(capsys, run_dir, "aov_bridge", "--period", "WTD", "--sql")
    assert code == 0
    assert "\nwith\n" in out and "-- parameters:" in out and "$cur_lo = " in out


def test_run_as_csv(capsys, run_dir):
    code, out, err = run(capsys, run_dir, "aov_bridge", "--period", "WTD", "--csv", "--sql")
    assert code == 0
    df = pd.read_csv(io.StringIO(out))
    assert df.iloc[-1].effect == 57_500 - 49_000
    assert "\nwith\n" in err  # the SQL goes to stderr, so stdout stays CSV


def test_run_facets_by_dimension(capsys, run_dir):
    code, out, _ = run(capsys, run_dir, "top_category_items", "--period", "L6W")
    assert code == 0
    headings = [line for line in out.splitlines() if line.startswith("== ")]
    title = "== Items of the top categories"
    assert headings == [f"{title} · Coffee", f"{title} · Snacks (Focaccia)"]
    code, out, _ = run(capsys, run_dir, "top_category_items", "--period", "L6W", "--csv")
    assert set(pd.read_csv(io.StringIO(out))["facet"]) == {"Coffee", "Snacks (Focaccia)"}


def test_run_facets_by_metric_print_their_result_once(capsys, run_dir):
    code, out, _ = run(capsys, run_dir, "kpi_cards", "--period", "L14D")
    assert code == 0
    assert out.count("== ") == 1
    assert "== Key figures · Net Sales, Orders, Avg Order Value, Items Sold" in out


def test_run_binds_parameters(capsys, run_dir):
    code, _, err = run(capsys, run_dir, "category_trend")
    assert code == 1 and r"$category are not bound" in err
    code, out, err = run(capsys, run_dir, "category_trend", "--param", "category=Coffee")
    assert code == 0, err
    code, _, err = run(capsys, run_dir, "aov_bridge", "--param", "category=Coffee")
    assert code == 0 and "Note: aov_bridge does not use $category" in err


def test_run_fails_before_building_on_a_bad_period(capsys, run_dir):
    code, _, err = run(capsys, run_dir, "aov_bridge", "--period", "L6X")
    assert code == 1 and "unknown window 'L6X'" in err and "Run " not in err


def test_run_an_unknown_component(capsys, run_dir):
    code, _, err = run(capsys, run_dir, "aov_bridg")
    assert code == 1 and "no component aov_bridg" in err and "did you mean aov_bridge?" in err


def test_run_parameters_are_yaml(capsys):
    assert cli.parse_params(["n=5", "codes=[Cash, BCA]", "category=Coffee"]) == {
        "n": 5,
        "codes": ["Cash", "BCA"],
        "category": "Coffee",
    }
    with pytest.raises(ValueError, match="write name=value"):
        cli.parse_params(["Category"])


# --- validate -------------------------------------------------------------------------------------


def test_validate(capsys):
    code, out, _ = component_cli(capsys, "validate")
    assert code == 0, out
    assert "ok   category_trend (" in out and "checked without $category" in out
    assert "6 component(s), 0 report(s), 0 problem(s)" in out


def test_validate_the_packaged_components(capsys):
    code, out, _ = component_cli(capsys, "validate", components=cli.components_dir())
    assert code == 0, out
    assert "ok   category_contributive_aov\n" in out and "ok   full_history_kpis\n" in out


def test_validate_lists_every_broken_component(capsys, tmp_path):
    shutil.copy(COMPONENTS / "aov_bridge.yml", tmp_path)
    (tmp_path / "broken.yml").write_text("id: broken\ntitle: x\nquery: {model: fct_orders}\n")
    (tmp_path / "wrong.yml").write_text("id: wrong\ntitle: x\nquery: {source: nope}\n")
    code, out, _ = component_cli(capsys, "validate", components=tmp_path)
    assert code == 1
    assert "FAIL components/broken.yml" in out and "FAIL components/wrong.yml" in out
    assert "ok   aov_bridge" in out and "3 component(s), 0 report(s), 2 problem(s)" in out


# --- previews (plan 8.6) --------------------------------------------------------------------------


def size(path):
    with Image.open(path) as image:
        return image.size


def test_run_png_draws_the_component_into_its_slot(capsys, run_dir, tmp_path):
    png = tmp_path / "kpis.png"
    code, out, err = run(capsys, run_dir, "kpi_cards", "--period", "L14D", "--png", str(png))
    assert code == 0, err
    assert out == f"Wrote {png}\n"  # a picture instead of the table
    width, height = size(png)
    assert 1500 < width and 0.2 < height / width < 0.5  # the slot: full width, half the body


def test_run_png_defaults_to_the_preview_folder(capsys, run_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, run_dir, "aov_bridge", "--period", "WTD", "--png")
    assert code == 0, err
    assert out == f"Wrote {cli.PREVIEW_DIR / 'aov_bridge.png'}\n"
    assert (tmp_path / "data/output/preview/aov_bridge.png").exists()


def test_span_and_height_set_the_size_of_the_slot(capsys, run_dir, tmp_path):
    def draw(name, span, height):
        png = tmp_path / name
        code, _, err = run(
            capsys, run_dir, "kpi_cards", "--png", str(png), "--span", span, "--height", height
        )
        assert code == 0, err
        return size(png)

    wide, narrow, tall = (
        draw("w.png", "12", "2"),
        draw("n.png", "10", "2"),
        draw("t.png", "12", "4"),
    )
    assert narrow[0] < wide[0] and tall[1] > wide[1]


def test_a_slot_too_small_for_the_visual_is_refused_as_in_a_report(capsys, run_dir, tmp_path):
    png = tmp_path / "small.png"
    args = ("aov_bridge", "--period", "WTD", "--png", str(png), "--span", "2", "--height", "1")
    code, _, err = run(capsys, run_dir, *args)
    assert code == 1 and not png.exists()
    assert "needs at least" in err and "(--span 2, --height 1)" in err


def test_a_text_that_runs_over_the_slot_is_a_warning(capsys, run_dir, tmp_path, folders):
    components, _ = folders
    path = components / "aov_bridge.yml"
    title = "Why the average order value moved"
    assert title in path.read_text()
    path.write_text(path.read_text().replace(title, title + " and then some" * 12))
    code, out, err = component_cli(
        capsys, "run", "aov_bridge", "--period", "WTD", "--run", str(run_dir), "--config",
        str(FIXTURES), "--png", str(tmp_path / "x.png"), components=components,
    )  # fmt: skip
    assert code == 0 and out.startswith("Wrote ")
    assert "Warning: " in err and "and then some" in err and "runs over the right edge" in err


@pytest.mark.parametrize(
    ("flag", "value"), [("--span", "0"), ("--span", "13"), ("--height", "0"), ("--height", "7")]
)
def test_span_and_height_have_limits(capsys, run_dir, tmp_path, flag, value):
    code, _, err = run(capsys, run_dir, "kpi_cards", "--png", str(tmp_path / "x.png"), flag, value)
    assert code == 1 and "1 to" in err


def test_watch_needs_png(capsys, run_dir):
    code, _, err = run(capsys, run_dir, "kpi_cards", "--watch")
    assert code == 1 and "--watch draws a PNG: add --png" in err


def test_a_component_without_a_visual_has_no_preview(capsys, run_dir, tmp_path, folders):
    components, _ = folders
    (components / "bare.yml").write_text("id: bare\ntitle: Bare\nquery: {source: lines_export}\n")
    png = str(tmp_path / "x.png")
    code, _, err = component_cli(
        capsys, "run", "bare", "--run", str(run_dir), "--config", str(FIXTURES), "--png", png,
        components=components,
    )  # fmt: skip
    assert code == 1 and "has no visual to preview" in err


def test_watch_draws_again_when_the_file_changes(tmp_path):
    target = tmp_path / "c.yml"
    target.write_text("v1")
    drawn, ticks = [], []

    def render():
        drawn.append(target.read_text())

    def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:  # saved between the second and the third look
            target.write_text("v2")
            os.utime(target, ns=(1, target.stat().st_mtime_ns + 5_000_000))

    err = io.StringIO()
    preview.watch(target, render, err, interval=0.1, sleep=sleep, stop=lambda: len(ticks) >= 4)
    assert drawn == ["v1", "v2"] and ticks == [0.1] * 4 and err.getvalue() == ""


def test_watch_survives_an_error_and_ends_on_ctrl_c(tmp_path):
    target = tmp_path / "c.yml"
    target.write_text("ok")
    calls = []

    def render():
        calls.append(target.read_text())
        if target.read_text() == "typo":
            raise ValueError("c.yml: query: no model 'x'")

    def sleep(seconds):
        if len(calls) == 1:
            target.write_text("typo")
            os.utime(target, ns=(1, target.stat().st_mtime_ns + 5_000_000))
        elif len(calls) == 2:
            target.write_text("fixed")
            os.utime(target, ns=(1, target.stat().st_mtime_ns + 9_000_000))
        else:
            raise KeyboardInterrupt

    err = io.StringIO()
    preview.watch(target, render, err, sleep=sleep)
    assert calls == ["ok", "typo", "fixed"]
    assert "Error: c.yml: query: no model 'x'" in err.getvalue()


def test_run_watch_hands_the_file_and_the_drawing_to_the_watcher(
    capsys, run_dir, tmp_path, folders
):
    components, _ = folders
    seen = {}

    def fake_watch(target, draw, err):
        seen["target"] = target
        draw()

    import argparse

    args = argparse.Namespace(
        id="kpi_cards", png=str(tmp_path / "w.png"), watch=True, span=12, height=3
    )
    db = open_run(run_dir, FIXTURES, None)
    ctx = QueryContext.create(db.con, run=db.run, checks=db.checks)
    out, err = io.StringIO(), io.StringIO()
    code = cli.cmd_preview(args, components, ctx, {"period": "L14D"}, out, err, watch=fake_watch)
    assert code == 0 and seen["target"] == components / "kpi_cards.yml"
    assert (tmp_path / "w.png").exists() and "Watching" in err.getvalue()


# --- new ------------------------------------------------------------------------------------------


@pytest.fixture
def folders(tmp_path):
    components, snapshots = tmp_path / "components", tmp_path / "snapshots"
    shutil.copytree(COMPONENTS, components)
    snapshots.mkdir()
    return components, snapshots


def new(capsys, folders, *args):
    components, snapshots = folders
    return component_cli(capsys, "new", *args, "--snapshots", str(snapshots), components=components)


def test_new_from_the_template(capsys, folders):
    code, out, err = new(capsys, folders, "weekly_basics")
    assert code == 0, err
    text = (folders[0] / "weekly_basics.yml").read_text()
    assert text.startswith(SCHEMA_REF + "\nid: weekly_basics\ntitle: Weekly basics\n")
    assert load_component(folders[0] / "weekly_basics.yml").bind().shape == "single"
    snapshot = read_snapshot((folders[1] / "weekly_basics.yml").read_text())
    assert snapshot == {"component": "weekly_basics", "params": {}}
    assert "--update-snapshots -k weekly_basics" in out


def test_new_from_a_component(capsys, folders):
    code, _, err = new(capsys, folders, "aov_bridge_l14d", "--from", "aov_bridge")
    assert code == 0, err
    copy = yaml.safe_load((folders[0] / "aov_bridge_l14d.yml").read_text())
    original = yaml.safe_load((COMPONENTS / "aov_bridge.yml").read_text())
    assert copy == {**original, "id": "aov_bridge_l14d"}


def test_new_with_a_parameter_without_a_default(capsys, folders):
    code, _, err = new(capsys, folders, "mains_trend", "--from", "category_trend")
    assert code == 1 and "--param category=..." in err
    assert not (folders[0] / "mains_trend.yml").exists()  # removed again
    code, _, err = new(capsys, folders, "mains_trend", "--from", "category_trend",
                       "--param", "category=Mains")  # fmt: skip
    assert code == 0, err
    snapshot = read_snapshot((folders[1] / "mains_trend.yml").read_text())
    assert snapshot["params"] == {"category": "Mains"}


def test_new_refuses_a_bad_or_existing_id(capsys, folders):
    assert new(capsys, folders, "AovBridge")[0] == 1
    code, _, err = new(capsys, folders, "aov_bridge")
    assert code == 1 and "exists already" in err
    code, _, err = new(capsys, folders, "x", "--from", "nope")
    assert code == 1 and "no component nope" in err


# --- schema ---------------------------------------------------------------------------------------


def test_schema(capsys, tmp_path, monkeypatch):
    code, out, _ = component_cli(capsys, "schema", "--check")
    assert code == 0 and out.count("is up to date") == 3  # component, report and bundle
    path = tmp_path / "component.schema.json"
    monkeypatch.setattr(components, "schema_path", lambda: path)
    code, out, _ = component_cli(capsys, "schema", "--check")
    assert code == 1 and "is out of date" in out
    code, out, _ = component_cli(capsys, "validate")
    assert code == 1 and "FAIL schema/component.schema.json is out of date" in out
    code, out, _ = component_cli(capsys, "schema")
    assert code == 0 and path.read_text() == schema_text()
