"""Building a report (plan 8.5), and the exit of phase 8: a page with the two tracer components
renders to PDF, PNG and Telegram text without text overflow, and writes a bundle. The fixture
run holds 4 orders from 5 to 13 October 2026."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest
import yaml
from PIL import Image

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.orchestration import report_cli
from hari_lari_analytics.presentation.builder import (
    BuildError,
    DataResolver,
    build_report,
    check_length,
    run_report,
)
from hari_lari_analytics.presentation.components import load_components
from hari_lari_analytics.presentation.outputs import png_pages
from hari_lari_analytics.presentation.outputs.telegram import fill_drive_url
from hari_lari_analytics.presentation.outputs.xlsx import sheet_name
from hari_lari_analytics.presentation.reports import (
    DRIVE_PLACEHOLDER,
    expand,
    parse_report,
)
from hari_lari_analytics.warehouse.db import open_run

from .conftest import FIXTURES

REPORTS = FIXTURES / "reports"
ANCHOR = date(2026, 10, 13)  # the last day of the fixture data
GENERATED = datetime(2026, 10, 14, 7, 0)
COMPONENTS = load_components()


def context(run_dir, anchor=ANCHOR):
    db = open_run(run_dir, FIXTURES, anchor)
    return QueryContext.create(db.con, run=db.run, checks=db.checks)


@pytest.fixture(scope="module")
def ctx(run_dir):
    return context(run_dir)


def tracer_spec(**changes):
    raw = yaml.safe_load((REPORTS / "tracer.yml").read_text())
    return {**raw, **changes}


def make(**changes):
    return parse_report(tracer_spec(**changes), "reports/tracer.yml", COMPONENTS)


@pytest.fixture(scope="module")
def built(ctx, tmp_path_factory):
    report = make()
    out = tmp_path_factory.mktemp("tracer")
    return build_report(report, ctx, out, GENERATED)


# --- the exit of phase 8 --------------------------------------------------------------------------


def test_the_tracer_page_renders_to_every_output_without_overflow(built):
    assert built.overflows == [], [o.describe() for o in built.overflows]
    names = sorted(p.name for p in built.files)
    assert names == [
        "bundle.json",
        "summary.txt",
        "tracer_2026-10-13.pdf",
        "tracer_2026-10-13.xlsx",
        "tracer_2026-10-13_overview.png",
    ]
    assert all(p.stat().st_size > 0 for p in built.files)


def test_a_component_note_is_rendered_and_in_the_bundle(built):
    kpis, table = built.run.pages[0].slots
    assert table.note.startswith("Reading a change in contributive AOV: ")
    assert table.note.count("\n") == 2  # the line breaks of the YAML are kept
    assert kpis.note == ""
    slots = json.loads(built.bundle.read_text())["pages"][0]["slots"]
    assert [s["note"] for s in slots] == [kpis.note, table.note]


def test_the_pdf_has_one_page_per_report_page(built):
    data = built.pdf.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", data)) == 1


def test_the_png_is_an_a4_landscape_page(built):
    with Image.open(built.pngs["overview"]) as image:
        width, height = image.size
    assert width / height == pytest.approx(11.69 / 8.27, abs=0.01)
    assert width > 1500


def test_the_telegram_text_keeps_the_drive_link_for_the_deliver_step(built):
    text = built.texts["summary"].read_text()
    assert text.startswith("Tracer report, week 2026-W42\n")
    assert "Net Sales: 213,000 IDR (n=4)" in text and "Orders: 4 (n=4)" in text
    assert text.rstrip().endswith(f"PDF and Excel lines: {DRIVE_PLACEHOLDER}")
    assert len(text) <= 1024


def test_the_bundle_describes_the_pages_and_their_results(built):
    bundle = json.loads(built.bundle.read_text())
    assert bundle["format_version"] == 1
    assert bundle["report"]["id"] == "tracer" and bundle["report"]["anchor"] == "2026-10-13"
    assert bundle["texts"] == {"summary": built.run.texts["summary"]}
    (page,) = bundle["pages"]
    assert page["id"] == "overview" and page["period"] == "L14D"
    assert [s["component"] for s in page["slots"]] == [
        "full_history_kpis",
        "category_contributive_aov",
    ]
    table = page["slots"][1]
    assert table["position"] and table["results"] and table["facets"]


def test_the_workbook_has_one_sheet_per_component_and_the_lines_export(built):
    book = openpyxl.load_workbook(built.xlsx)
    assert book.sheetnames == ["category_contributive_aov", "Sales Lines", "Column Guide"]
    table = book["category_contributive_aov"]
    header = [c.value for c in table[1]]
    assert header[:3] == ["Category", "Is total", "Net Sales (IDR)"]
    assert table.max_row == 7  # the header, five categories and the total
    assert book["Sales Lines"].max_row == 10  # the header and the 9 lines of the fixture


def test_sheet_names_are_valid_and_unique():
    assert sheet_name("a/b:c*d", []) == "a b c d"
    assert sheet_name("x" * 40, []) == "x" * 31
    assert sheet_name("Sales", ["sales"]) == "Sales 2"
    assert sheet_name("x" * 40, ["x" * 31]) == "x" * 29 + " 2"


# --- the run --------------------------------------------------------------------------------------


def test_a_run_holds_pages_texts_and_sheets_as_data(ctx):
    run = run_report(make(), ctx, GENERATED)
    assert run.anchor == ANCHOR and run.generated == GENERATED
    (page,) = run.pages
    assert [c[0] for c in page.chips] == [
        "Last 14 days",
        "30 Sep 2026 – 13 Oct 2026",
        "no comparison",
    ]
    assert page.note == "The prior period starts before the first sale (05 Oct 2026)."
    assert [s.title for s in page.slots][0] == "Full history"
    assert [s.ref.bound.id for s in run.sheets] == ["category_contributive_aov", "lines_export"]
    assert (
        run.footer
        == "Test café · fixture data · data through 13 Oct 2026 · generated 14 Oct 2026 07:00"
    )


def test_png_pages_follow_the_base_id_of_repeated_pages(ctx):
    pages = [{**tracer_spec()["pages"][0], "title": "{{ period }}"}]
    repeated = [{"repeat": {"period": ["L14D", "L6W"]}, "pages": pages}]
    run = run_report(make(pages=repeated, outputs={"png": ["overview"]}, deliver={}), ctx)
    assert png_pages(run) == ["overview_l14d", "overview_l6w"]
    run = run_report(make(pages=repeated, outputs={"png": "all"}, deliver={}), ctx)
    assert png_pages(run) == ["overview_l14d", "overview_l6w"]
    run = run_report(make(pages=repeated, outputs={}, deliver={}), ctx)
    assert png_pages(run) == []


# --- repeats over data ----------------------------------------------------------------------------


def category_pages(**repeat):
    page = {
        "id": "cat",
        "title": "{{ category }}",
        "grid": {"columns": 12, "rows": [1]},
        "slots": [
            {
                "component": "category_contributive_aov",
                "row": 1,
                "col": 1,
                "span": 12,
                "with": {"query": {"focus": {"adjusted_category": ["$category"]}}},
            }
        ],
    }
    top = {"dimension": "adjusted_category", "top": 2, "by": "net_sales", "window": "$period"}
    return [{"repeat": {"category": {**top, **repeat}}, "pages": [page]}]


def test_a_repeat_over_the_top_members_asks_the_data(ctx):
    report = make(pages=category_pages(), outputs={}, text_blocks={}, deliver={})
    pages = expand(report, ANCHOR, DataResolver(ctx))
    assert [p.title for p in pages] == ["Coffee", "Mains"]  # top 2 of L14D by net sales
    assert [p.id for p in pages] == ["cat_coffee", "cat_mains"]
    assert [p.params["category"] for p in pages] == ["Coffee", "Mains"]


def test_the_deep_dive_form_filters_every_page_to_its_repeated_value(ctx):
    """The category deep dive of docs/components.md: `focus: { adjusted_category: $category }`
    at the top of the report, one page per top category."""
    page = category_pages()[0]["pages"][0]
    page["slots"] = [{k: v for k, v in page["slots"][0].items() if k != "with"}]
    top = category_pages()[0]["repeat"]
    report = make(
        pages=[{"repeat": top, "pages": [page]}],
        filters={"focus": {"adjusted_category": "$category"}},
        outputs={},
        text_blocks={},
        deliver={},
    )
    run = run_report(report, ctx, GENERATED)
    members = [list(p.slots[0].facets[0].result.rows["adjusted_category"]) for p in run.pages]
    assert members == [["Coffee"], ["Mains"]]


def test_the_top_members_follow_the_where_filter_of_the_report(ctx):
    report = make(
        pages=category_pages(top=5),
        filters={"where": {"payment_type": ["Cash"]}},
        outputs={},
        text_blocks={},
        deliver={},
    )
    everyone = expand(
        make(pages=category_pages(top=5), outputs={}, text_blocks={}, deliver={}),
        ANCHOR,
        DataResolver(ctx),
    )
    cash = expand(report, ANCHOR, DataResolver(ctx))
    assert len(cash) <= len(everyone) == 5


def test_a_period_without_sales_gets_the_when_empty_page(run_dir, tmp_path):
    pages = tracer_spec()["pages"]
    repeated = [
        {
            "repeat": {"period": ["L14D"]},
            "when_empty": {"title": "Overview", "message": "No sales in {{ period }}"},
            "pages": pages,
        }
    ]
    report = make(
        pages=repeated, outputs={"pdf": True, "png": ["overview"]}, text_blocks={}, deliver={}
    )
    quiet = context(run_dir, date(2026, 11, 24))  # six weeks after the last sale
    run = run_report(report, quiet, GENERATED)
    (page,) = run.pages
    assert page.page.id == "empty_l14d" and page.page.empty_message == "No sales in L14D"
    result = build_report(report, quiet, tmp_path, GENERATED)
    assert result.pngs == {}  # an empty page is not written as PNG
    assert result.pdf.exists() and result.overflows == []


def test_a_build_over_empty_windows_still_writes_every_output(run_dir, tmp_path):
    quiet = context(run_dir, date(2026, 11, 24))
    result = build_report(make(), quiet, tmp_path, GENERATED)
    assert result.pdf.exists() and result.xlsx.exists() and result.overflows == []
    assert json.loads(result.bundle.read_text())["pages"]


# --- filters in the results -----------------------------------------------------------------------


def test_a_report_filter_narrows_the_results_and_shows_as_a_chip(ctx):
    report = make(
        filters={"focus": {"adjusted_category": ["Coffee"]}}, outputs={}, text_blocks={}, deliver={}
    )
    run = run_report(report, ctx, GENERATED)
    (page,) = run.pages
    assert page.chips[-1] == ("Category: Coffee (focus)", "line")
    table = page.slots[1].facets[0].result
    assert list(table.rows["adjusted_category"]) == ["Coffee"]


def test_a_slot_the_filter_does_not_reach_says_so_in_its_hint(ctx):
    slots = tracer_spec()["pages"][0]["slots"]
    slots[0] = {**slots[0], "ignore_filters": True}
    page = {**tracer_spec()["pages"][0], "slots": slots}
    filters = {"where": {"payment_type": ["Cash"]}}
    report = make(filters=filters, pages=[page], outputs={}, text_blocks={}, deliver={})
    kpis, table = run_report(report, ctx, GENERATED).pages[0].slots
    assert kpis.hint.endswith(" · not filtered by Payment type")
    assert kpis.hint.startswith("05 Oct 2026 – 13 Oct 2026")  # the component's own hint first
    assert "not filtered" not in table.hint  # this slot is filtered


# --- the Telegram limits --------------------------------------------------------------------------


def test_a_text_over_the_caption_limit_fails_the_build(ctx):
    long = {"text": "x" * 1100}
    report = make(
        text_blocks={"summary": [long]},
        outputs={"telegram": ["summary"], "png": ["overview"]},
        deliver={},
    )
    with pytest.raises(
        BuildError, match=r"text block summary has 1100 characters; Telegram takes at most 1024"
    ):
        run_report(report, ctx)


def test_the_drive_link_counts_for_a_hundred_characters():
    report = make()
    limit = 1024
    filler = "x" * (limit - 100 - len(" "))
    check_length(report, "summary", f"{filler} {DRIVE_PLACEHOLDER}")  # exactly at the limit
    with pytest.raises(BuildError, match="1025 characters"):
        check_length(report, "summary", f"{filler}x {DRIVE_PLACEHOLDER}")


def test_a_block_sent_alone_may_use_the_message_limit(ctx):
    deliver = {"telegram": [{"to": "group", "text": "summary"}]}
    long = {"text": "x" * 3000}
    report = make(
        text_blocks={"summary": [long]}, outputs={"telegram": ["summary"]}, deliver=deliver
    )
    assert run_report(report, ctx).texts["summary"] == "x" * 3000


def test_filling_in_the_drive_link():
    text = f"Net sales: 1\n\nPDF: {DRIVE_PLACEHOLDER}\nDone"
    assert fill_drive_url(text, "https://drive/x") == "Net sales: 1\n\nPDF: https://drive/x\nDone"
    assert fill_drive_url(text, None) == "Net sales: 1\n\nDone"  # no bare placeholder is sent


# --- the overflow check, end to end ---------------------------------------------------------------


def test_a_title_that_does_not_fit_its_slot_is_reported(ctx, tmp_path):
    slots = tracer_spec()["pages"][0]["slots"]
    slots[1] = {**slots[1], "span": 6, "with": {"title": "An extremely long title " * 8}}
    page = {**tracer_spec()["pages"][0], "slots": slots}
    report = make(pages=[page], outputs={"pdf": True}, text_blocks={}, deliver={})
    result = build_report(report, ctx, tmp_path, GENERATED)
    assert result.overflows
    assert all("category_contributive_aov" in o.where for o in result.overflows)
    assert any("An extremely long title" in o.text for o in result.overflows)


# --- the CLI --------------------------------------------------------------------------------------


def cli(capsys, *args):
    code = report_cli.main(["--reports", str(REPORTS), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def build(capsys, run_dir, out, *args, report="tracer"):
    return cli(
        capsys, "build", "--report", report, "--run", str(run_dir), "--config", str(FIXTURES),
        "--anchor", "2026-10-13", "--out", str(out), *args,
    )  # fmt: skip


def test_build_writes_the_outputs_and_lists_them(capsys, run_dir, tmp_path):
    code, out, err = build(capsys, run_dir, tmp_path)
    assert code == 0, err
    assert "Run 2026-10-14, data through 13 Oct 2026" in err
    listed = [Path(line).name for line in out.splitlines()]
    assert "tracer_2026-10-13.pdf" in listed and "bundle.json" in listed
    assert all((tmp_path / name).exists() for name in listed)


def test_build_without_out_writes_under_data_output(capsys, run_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = build(capsys, run_dir, tmp_path / "x")  # an explicit --out is honoured
    assert code == 0 and str(tmp_path / "x") in out
    code, _, err = cli(
        capsys, "build", "--report", "tracer", "--run", str(run_dir), "--config", str(FIXTURES),
        "--anchor", "2026-10-13",
    )  # fmt: skip
    assert code == 0, err
    assert (tmp_path / "data/output/reports/tracer/2026-10-13/bundle.json").exists()


def test_build_takes_the_path_of_a_report_file(capsys, run_dir, tmp_path):
    code, _, err = build(capsys, run_dir, tmp_path, report=str(REPORTS / "tracer.yml"))
    assert code == 0, err


def test_build_names_an_unknown_report(capsys, run_dir, tmp_path):
    code, _, err = build(capsys, run_dir, tmp_path, report="nope")
    assert code == 1 and "no report nope" in err and "known: tracer" in err


def test_strict_fails_on_an_overflow(capsys, run_dir, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    slots = tracer_spec()["pages"][0]["slots"]
    slots[1] = {**slots[1], "span": 6, "with": {"title": "An extremely long title " * 8}}
    bad = tracer_spec(pages=[{**tracer_spec()["pages"][0], "slots": slots}])
    (reports / "tracer.yml").write_text(yaml.safe_dump(bad))
    out = str(tmp_path / "o")
    args = ("--reports", str(reports), "build", "--report", "tracer", "--run", str(run_dir),
            "--config", str(FIXTURES), "--anchor", "2026-10-13", "--out", out)  # fmt: skip
    code = report_cli.main(args)
    err = capsys.readouterr().err
    assert code == 0 and "Warning: " in err and "An extremely long title" in err
    code = report_cli.main([*args, "--strict"])
    assert code == 1 and "text(s) run over their slots" in capsys.readouterr().err


def test_validate_lists_the_reports(capsys):
    code, out, _ = cli(capsys, "validate")
    assert code == 0, out
    count = len(load_components())
    assert (
        out.startswith("ok   tracer (1 page(s)")
        and f"1 report(s), {count} component(s), 0 problem(s)" in out
    )


def test_validate_reports_every_broken_report(capsys, tmp_path):
    (tmp_path / "broken.yml").write_text("id: broken\ntitle: x\npages: []\n")
    (tmp_path / "tracer.yml").write_text((REPORTS / "tracer.yml").read_text())
    code = report_cli.main(["--reports", str(tmp_path), "validate"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL reports/broken.yml" in out and "ok   tracer" in out and "1 problem(s)" in out


def test_validate_names_the_slots_a_filter_does_not_reach(capsys, tmp_path):
    filters = {"where": {"payment_type": ["Cash"]}}
    slots = tracer_spec()["pages"][0]["slots"]
    slots[0] = {**slots[0], "ignore_filters": True}
    page = {**tracer_spec()["pages"][0], "slots": slots}
    (tmp_path / "tracer.yml").write_text(yaml.safe_dump(tracer_spec(filters=filters, pages=[page])))
    code = report_cli.main(["--reports", str(tmp_path), "validate"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "filters.where.payment_type does not apply to slot(s): overview/1" in out


def test_schedules_lists_the_reports_that_have_one(capsys, tmp_path):
    scheduled = tracer_spec(schedule={"cron": "0 7 * * 1", "timezone": "Asia/Jakarta"})
    (tmp_path / "tracer.yml").write_text(yaml.safe_dump(scheduled))
    other = {**tracer_spec(id="quiet"), "outputs": {}, "text_blocks": {}, "deliver": {}}
    (tmp_path / "quiet.yml").write_text(yaml.safe_dump(other))
    code = report_cli.main(["--reports", str(tmp_path), "schedules", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [(s["report"], s["cron"], s["timezone"], s["enabled"]) for s in found] == [
        ("tracer", "0 7 * * 1", "Asia/Jakarta", True)
    ]
    code = report_cli.main(["--reports", str(tmp_path), "schedules"])
    assert "tracer" in capsys.readouterr().out and code == 0


def test_the_schemas_are_up_to_date(capsys):
    code, out, _ = cli(capsys, "schema", "--check")
    assert code == 0 and out.count("is up to date") == 3


def test_the_report_command_is_the_reporting_cli():
    import tomllib

    scripts = tomllib.loads(Path("pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["hari-lari-report"] == "hari_lari_analytics.orchestration.report_cli:main"
    assert "hari-lari-weekly" not in scripts and "hari-lari-report-legacy" not in scripts
