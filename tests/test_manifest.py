"""The semantic manifest (plan 8.10): the catalogue of the semantic layer, current and complete."""

from __future__ import annotations

import json
import typing

from hari_lari_analytics.analysis import transforms, windows
from hari_lari_analytics.orchestration import cli
from hari_lari_analytics.presentation import manifest, visuals
from hari_lari_analytics.presentation.components import load_components
from hari_lari_analytics.presentation.insights import default_rules
from hari_lari_analytics.presentation.manifest import build_manifest, manifest_path, manifest_text
from hari_lari_analytics.semantic.registry import default_registry

from .components_fixture import all_components
from .test_report_build import make

REGISTRY = default_registry()
PACKAGED = json.loads(manifest_path().read_text())


# --- the file -----------------------------------------------------------------------------------


def test_the_manifest_file_is_up_to_date():
    assert manifest_path().read_text() == manifest_text(), (
        "run `uv run hari-lari-component manifest` and commit the result"
    )


def test_the_manifest_is_deterministic():
    assert manifest_text() == manifest_text()


def test_the_manifest_is_valid_json_with_a_version():
    assert PACKAGED["format_version"] == manifest.FORMAT_VERSION
    assert set(PACKAGED) == {
        "format_version",
        "project",
        "models",
        "metrics",
        "default_min_n",
        "dimensions",
        "kpi_tree",
        "windows",
        "transforms",
        "visuals",
        "insight_rules",
        "components",
        "reports",
    }


# --- it lists everything ------------------------------------------------------------------------


def ids(section: str, key: str = "id") -> list[str]:
    return [item[key] for item in PACKAGED[section]]


def test_every_model_metric_dimension_and_identity_is_listed():
    assert ids("models") == list(REGISTRY.models)
    assert ids("metrics") == list(REGISTRY.metrics)
    assert ids("dimensions") == list(REGISTRY.dimensions)
    assert ids("kpi_tree") == list(REGISTRY.identities)


def test_every_transform_visual_and_insight_rule_is_listed():
    assert sorted(ids("transforms", "name")) == sorted(transforms.TRANSFORMS)
    assert sorted(ids("visuals", "name")) == sorted(visuals.VISUALS)
    assert ids("insight_rules", "name") == list(default_rules())


def test_every_packaged_component_is_listed():
    assert ids("components") == sorted(load_components())


def test_a_metric_has_what_a_frontend_shows():
    metric = next(m for m in PACKAGED["metrics"] if m["id"] == "net_sales")
    assert metric["label"] == "Net Sales" and metric["unit"] == "IDR"
    assert metric["format"] == "idr" and metric["kind"] == "sql" and metric["up_is_good"] is True
    assert metric["description"] and metric["additivity"] == "full"
    assert "fct_sales_lines" in metric["models"]
    aov = next(m for m in PACKAGED["metrics"] if m["id"] == "aov")
    assert aov["kind"] == "derived" and aov["depends_on"] == ["net_sales", "orders"]
    assert aov["expression"] and "aggregates" not in aov


def test_a_model_lists_what_can_be_asked_of_it():
    orders = next(m for m in PACKAGED["models"] if m["id"] == "fct_orders")
    assert orders["exposed"] and orders["sample_size"] == "orders" and orders["calendar"]
    assert "aov" in orders["metrics"] and "weekday" in orders["dimensions"]
    staging = next(m for m in PACKAGED["models"] if m["id"].startswith("stg_"))
    assert not staging["exposed"] and staging["metrics"] == []


def test_a_dimension_has_its_labels_and_bins():
    by_id = {d["id"]: d for d in PACKAGED["dimensions"]}
    assert by_id["weekday"]["labels"]["1"] == "Mon"
    assert by_id["basket_size"]["bins"][-1] == "5+"
    assert by_id["adjusted_category"]["models"]


def test_the_manifest_is_consistent_with_itself():
    metrics = set(ids("metrics"))
    models = set(ids("models"))
    dimensions = set(ids("dimensions"))
    for m in PACKAGED["metrics"]:
        assert set(m["depends_on"]) <= metrics, m["id"]
        assert set(m["models"]) <= models, m["id"]
    for d in PACKAGED["dimensions"]:
        assert set(d["models"]) <= models, d["id"]
    for model in PACKAGED["models"]:
        assert set(model["metrics"]) <= metrics and set(model["dimensions"]) <= dimensions
    for identity in PACKAGED["kpi_tree"]:
        used = [identity["kpi"], *identity.get("drivers", []), identity.get("driver", "net_sales")]
        assert {u.lstrip("-") for u in used} <= metrics, identity["id"]
    for component in PACKAGED["components"]:
        assert component["visual"] is None or component["visual"] in ids("visuals", "name")
        assert set(component["transforms"]) <= set(ids("transforms", "name"))
        assert set(component["insights"]) <= set(ids("insight_rules", "name"))


def test_the_window_forms_parse_and_the_comparisons_are_those_of_the_compiler():
    for form in PACKAGED["windows"]["forms"]:
        windows.parse_window(form["example"])
        assert form["form"] and form["description"]
    assert set(PACKAGED["windows"]["compare"]) == set(typing.get_args(windows.Compare))


def test_the_options_of_transforms_and_visuals_are_named():
    visual = next(v for v in PACKAGED["visuals"] if v["name"] == "table")
    assert {"bars", "deltas", "rank"} <= set(visual["options"])
    assert visual["accepts"] and len(visual["min_size"]) == 2 and visual["description"]
    transform = next(t for t in PACKAGED["transforms"] if t["name"] == "decompose")
    assert "identity" in transform["options"] and transform["shapes"]


# --- components and reports ---------------------------------------------------------------------


def test_a_component_lists_its_query_parameters_and_visual():
    comp = next(c for c in PACKAGED["components"] if c["id"] == "category_contributive_aov")
    assert comp["params"] == {"period": "L6W"} and comp["unbound"] == []
    assert comp["query"]["model"] == "fct_sales_lines" and comp["query"]["window"] == "$period"
    assert comp["visual"] == "table" and comp["title"]
    source = next(c for c in PACKAGED["components"] if c["id"] == "lines_export")
    assert source["query"] == {"source": "lines_export"} and source["visual"] is None


def test_a_parameter_without_a_default_is_listed_as_unbound():
    raw = build_manifest(all_components(), {})
    trend = next(c for c in raw["components"] if c["id"] == "category_trend")
    assert trend["unbound"] == ["category"]
    assert [c["id"] for c in raw["components"]] == sorted(all_components())
    assert raw["reports"] == []


def test_a_report_lists_its_schedule_outputs_and_pages():
    report = make(
        schedule={"cron": "0 4 * * 1"},
        deliver={},
    )
    raw = build_manifest(all_components(), {"tracer": report})
    (entry,) = raw["reports"]
    assert entry["id"] == "tracer" and entry["schedule"]["cron"] == "0 4 * * 1"
    assert entry["outputs"]["pdf"] is True and entry["outputs"]["png"] == ["overview"]
    assert entry["pages"] == [
        {
            "id": "overview",
            "title": "Overview",
            "components": ["category_contributive_aov", "full_history_kpis"],
        }
    ]


def test_it_holds_no_business_identifier():
    text = manifest_text().lower()
    for word in ("telegram-", "telegram_targets", "secret", "chat_id", "gs://", "googleapis"):
        assert word not in text, word


# --- the commands -------------------------------------------------------------------------------


def run_cli(capsys, *args, components=None):
    argv = ["--components", str(components)] if components else []
    code = cli.main([*argv, *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_manifest_check_passes_on_the_packaged_file(capsys):
    code, out, _ = run_cli(capsys, "manifest", "--check", components=cli.components_dir())
    assert code == 0 and "semantic_manifest.json is up to date" in out


def test_manifest_writes_a_file_and_check_notices_a_change(capsys, tmp_path):
    target = tmp_path / "semantic_manifest.json"
    code, out, _ = run_cli(capsys, "manifest", "--out", str(target))
    assert code == 0 and out.startswith("Wrote ")
    assert json.loads(target.read_text())["format_version"] == 1
    assert run_cli(capsys, "manifest", "--out", str(target), "--check")[0] == 0
    target.write_text(target.read_text().replace('"Net Sales"', '"Revenue"'))
    code, out, _ = run_cli(capsys, "manifest", "--out", str(target), "--check")
    assert code == 1 and "is out of date" in out
    code, out, _ = run_cli(capsys, "manifest", "--out", str(tmp_path / "missing.json"), "--check")
    assert code == 1 and "is out of date" in out


def test_manifest_follows_the_components_folder(capsys, tmp_path):
    target = tmp_path / "m.json"
    run_cli(capsys, "manifest", "--out", str(target), components=cli.components_dir())
    assert ids_of(target) == sorted(load_components())


def ids_of(path):
    return [c["id"] for c in json.loads(path.read_text())["components"]]


def test_validate_fails_when_the_manifest_is_out_of_date(capsys, monkeypatch, tmp_path):
    stale = tmp_path / "semantic_manifest.json"
    stale.write_text("{}\n")
    monkeypatch.setattr(manifest, "manifest_path", lambda: stale)
    code, out, _ = run_cli(capsys, "validate", components=cli.components_dir())
    assert code == 1
    assert "FAIL semantic_manifest.json is out of date: run hari-lari-component manifest" in out


def test_validate_passes_with_the_packaged_manifest(capsys):
    code, out, _ = run_cli(capsys, "validate", components=cli.components_dir())
    assert code == 0, out


def test_the_command_is_documented_in_the_module():
    assert "hari-lari-component manifest" in (cli.__doc__ or "")
