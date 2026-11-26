"""Insight sentences on the charts (plan 9.8): the report option `insights: chart | none`, the
sentences at the foot of each card, the tag of a quiet chart, `insights_from`, the room the
layout keeps, and what the bundle says.

The fixture has no valid comparison, so a slot's sentences are put in by replacing the slot's
run (the rules themselves are tested in test_insights.py and test_insights_weekly.py)."""

from __future__ import annotations

import dataclasses
import json

import matplotlib
import pytest
import yaml
from matplotlib.text import Text

from hari_lari_analytics.reporting import builder, layouts, visuals
from hari_lari_analytics.reporting.builder import build_report, run_report, run_slot
from hari_lari_analytics.reporting.components import (
    ComponentError,
    check_sources,
    load_components,
    parse_component,
)
from hari_lari_analytics.reporting.insights import CHART_MAX, Insight, collect
from hari_lari_analytics.reporting.outputs.pages import NO_COMPARISON, QUIET, render_page
from hari_lari_analytics.reporting.reports import ReportError, parse_report
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME

from .test_report_build import COMPONENTS, GENERATED, context, tracer_spec


@pytest.fixture(scope="module")
def ctx(run_dir):
    return context(run_dir)


SENTENCE = "Mains +9,355 to the AOV, mostly in more orders (penetration +16.8 pp)"


def report(insights: str | None = None, **changes):
    raw = tracer_spec(**changes)
    if insights is not None:
        raw["insights"] = insights
    return parse_report(raw, "reports/tracer.yml", COMPONENTS)


def drawn(run, page, slot_id):
    """The texts a page draws, and the rectangle of the slot of this component."""
    slot = next(s for s in page.slots if s.slot.bound.id == slot_id)
    x, y, w, h = slot.slot.rect
    with matplotlib.rc_context(DEFAULT_THEME.rc()):
        fig, overflows = render_page(run, page, DEFAULT_THEME)
        inv = fig.transFigure.inverted()
        texts = []
        for t in fig.findobj(Text):
            if not t.get_text():
                continue
            extent = t.get_window_extent()
            cx, cy = inv.transform(((extent.x0 + extent.x1) / 2, (extent.y0 + extent.y1) / 2))
            if x <= cx <= x + w and y <= cy <= y + h:  # what this slot drew, not the chips
                texts.append((t.get_text(), extent))
    return texts, slot.slot.rect, overflows, fig


def said(run, slot_id, **fields):
    """The run with the slot's run replaced, as if its rules had spoken."""
    (page,) = run.pages
    slots = [
        dataclasses.replace(s, **fields) if s.slot.bound.id == slot_id else s for s in page.slots
    ]
    page = dataclasses.replace(page, slots=slots)
    return dataclasses.replace(run, pages=[page]), page


def tags(texts):
    return [t for t, _ in texts if t in (QUIET, NO_COMPARISON)]


# --- the option ------------------------------------------------------------------------------


def test_a_report_says_none_unless_it_asks_for_charts():
    assert report().spec.insights == "none"
    assert report("chart").spec.insights == "chart"
    with pytest.raises(ReportError, match="insights"):
        report("page")


def test_the_weekly_report_asks_for_charts():
    from hari_lari_analytics.reporting.reports import load_reports

    reports = load_reports(COMPONENTS)
    assert reports["weekly"].spec.insights == "chart"
    assert reports["stock_alert"].spec.insights == "none"  # an action list, not an analysis
    assert reports["category_deep_dive"].spec.insights == "none"


def test_none_draws_no_sentence_and_no_tag(ctx):
    run = run_report(report("none"), ctx, GENERATED)
    run, page = said(
        run,
        "category_contributive_aov",
        insights=[Insight(SENTENCE, 0.4, "category_slices", (None, "slice", "Mains"), "good")],
        status="found",
    )
    texts, _, overflows, _ = drawn(run, page, "category_contributive_aov")
    assert SENTENCE not in [t for t, _ in texts]
    run, page = said(run, "category_contributive_aov", insights=[], status="quiet")
    texts, *_ = drawn(run, page, "category_contributive_aov")
    assert tags(texts) == []


def test_none_still_runs_the_rules(ctx):
    """`insights` only decides what is drawn: the sentences are in the run (and so in the bundle,
    the text blocks and `deliver.when`)."""
    run = run_report(report("none"), ctx, GENERATED)
    slot = next(s for s in run.pages[0].slots if s.slot.bound.id == "category_contributive_aov")
    assert slot.status == "no_comparison"  # the rules ran: they had no comparison to read
    assert not slot.draw_insights


# --- drawn on the card -----------------------------------------------------------------------


def test_a_sentence_is_drawn_inside_its_own_slot_above_the_note(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    insight = Insight(SENTENCE, 0.4, "category_slices", (None, "slice", "Mains"), "good")
    run, page = said(run, "category_contributive_aov", insights=[insight], status="found")
    texts, rect, overflows, fig = drawn(run, page, "category_contributive_aov")
    assert overflows == []
    (found,) = [e for t, e in texts if t == SENTENCE]
    inv = fig.transFigure.inverted()
    (x0, y0), (x1, y1) = inv.transform([(found.x0, found.y0), (found.x1, found.y1)])
    x, y, w, h = rect
    assert x <= x0 and x1 <= x + w and y <= y0 and y1 <= y + h
    (note,) = [e for t, e in texts if t.startswith("Reading a change in contributive AOV")]
    assert found.y0 > note.y1 - 1  # above the note
    assert [t for t, _ in texts].count("●") == 1  # one mark per sentence
    assert tags(texts) == []  # a chart that spoke has no tag


def test_a_long_sentence_wraps_to_the_card(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    long = " ".join([SENTENCE] * 3)
    insight = Insight(long, 0.4, "category_slices", (None, "slice", "Mains"), "good")
    run, page = said(run, "category_contributive_aov", insights=[insight], status="found")
    texts, _, overflows, _ = drawn(run, page, "category_contributive_aov")
    assert overflows == []
    assert long not in [t for t, _ in texts]  # broken into lines of the card's width


def test_a_quiet_chart_shows_its_tag_in_the_card_header(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    run, page = said(run, "category_contributive_aov", insights=[], status="quiet")
    texts, rect, overflows, fig = drawn(run, page, "category_contributive_aov")
    assert overflows == []
    (tag,) = [e for t, e in texts if t == QUIET]
    inv = fig.transFigure.inverted()
    (x0, y0), (x1, y1) = inv.transform([(tag.x0, tag.y0), (tag.x1, tag.y1)])
    x, y, w, h = rect
    assert x1 <= x + w and y1 <= y + h and y0 > y + h - 0.05  # right-aligned in the header row


def test_a_chart_without_a_valid_comparison_says_no_comparison(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    texts, *_ = drawn(run, run.pages[0], "category_contributive_aov")
    assert tags(texts) == [NO_COMPARISON]  # the fixture has none


def test_a_chart_without_rules_shows_nothing(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    kpis = next(s for s in run.pages[0].slots if s.slot.bound.id == "full_history_kpis")
    assert kpis.status is None and kpis.insights == []


def test_a_slot_without_a_card_gets_its_sentences_under_its_cells_and_no_tag(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    slot = next(s for s in run.pages[0].slots if s.slot.bound.id == "full_history_kpis")
    assert not slot.slot.card
    insight = Insight("Net sales +4.0%: +1.22M from orders", 0.4, "x", (None, "net_sales"), "good")
    run2, page = said(run, "full_history_kpis", insights=[insight], status="found")
    texts, rect, overflows, fig = drawn(run2, page, "full_history_kpis")
    assert overflows == []
    assert "Net sales +4.0%: +1.22M from orders" in [t for t, _ in texts]
    run3, page = said(run, "full_history_kpis", insights=[], status="quiet")
    texts, *_ = drawn(run3, page, "full_history_kpis")
    assert tags(texts) == []  # a quiet slot without a card shows nothing


def test_a_faceted_slot_prefixes_each_sentence_with_its_facet(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    slot = next(s for s in run.pages[0].slots if s.slot.bound.id == "full_history_kpis")
    assert len(slot.facets) > 1
    insight = Insight("up 4.0%", 0.4, "x", ("Orders",), "good", facet="Orders")
    run2, page = said(run, "full_history_kpis", insights=[insight], status="found")
    texts, *_ = drawn(run2, page, "full_history_kpis")
    assert "Orders: up 4.0%" in [t for t, _ in texts]


def test_the_sentences_are_not_prefixed_when_there_is_one_facet(ctx):
    run = run_report(report("chart"), ctx, GENERATED)
    insight = Insight("Mains up", 0.4, "x", ("Orders",), "good", facet="Orders")
    run, page = said(run, "category_contributive_aov", insights=[insight], status="found")
    texts, *_ = drawn(run, page, "category_contributive_aov")
    assert "Mains up" in [t for t, _ in texts]


# --- collecting per chart --------------------------------------------------------------------


def found(n, score):
    return [
        Insight(f"{score} {i}", score - i / 100, "r", (None, f"k{i}"), "good") for i in range(n)
    ]


def test_a_chart_carries_its_two_best_sentences(ctx, monkeypatch):
    run = run_report(report("chart"), ctx, GENERATED)
    slot = run.pages[0].slots[1].slot
    candidates = found(4, 0.5) + [Insight("same key, weaker", 0.1, "r2", (None, "k0"))]
    monkeypatch.setattr(builder, "component_insights", lambda *a, **k: list(candidates))
    run_slot_ = run_slot(slot, ctx, insights="chart")
    assert CHART_MAX == 2
    assert [i.text for i in run_slot_.insights] == ["0.5 0", "0.5 1"]
    assert len(run_slot_.candidates) == 5  # nothing is lost for the page and `deliver.when`
    assert run_slot_.status == "found" and run_slot_.draw_insights


def test_the_page_collects_the_candidates_of_every_slot(ctx, monkeypatch):
    monkeypatch.setattr(builder, "component_insights", lambda *a, **k: found(3, 0.5))
    run = run_report(report("chart"), ctx, GENERATED)
    page = run.pages[0]
    every = [i for s in page.slots for i in s.candidates]
    assert page.insights == collect(every)
    assert all(len(s.insights) <= CHART_MAX for s in page.slots)


# --- insights_from ---------------------------------------------------------------------------


def _component(source: str, **fields):
    raw = {"id": "x", "title": "X", **yaml.safe_load(source), **fields}
    return parse_component(raw, f"components/{raw['id']}.yml")


ONLY = """
query: {model: fct_orders, metrics: [aov], window: $period, compare: prior}
transforms: [{decompose: {identity: aov_basket}}]
insights: [aov_drivers]
"""
HOST = """
params: {period: L6W}
query: {model: fct_orders, metrics: [orders], window: $period, compare: prior}
visual: {type: kpi_card}
insights_from: [only]
"""


def sources(**changes):
    only = _component(ONLY, id="only", params={"period": "L6W"}, **changes.pop("only", {}))
    host = _component(HOST, id="host", **changes)
    return {"only": only, "host": host}


def test_the_packaged_components_pass_the_source_check():
    check_sources(load_components())
    kpis = load_components()["period_kpis"]
    assert kpis.raw["insights_from"] == ["net_sales_drivers", "aov_drivers"]


def test_a_source_is_checked():
    check_sources(sources())


def test_an_unknown_source_fails_with_the_known_ones():
    found = sources(insights_from=["nope"])
    with pytest.raises(ComponentError, match="insights_from: nope: no such component"):
        check_sources(found)


def test_a_source_with_a_visual_fails():
    found = sources(only={"visual": {"type": "waterfall"}})
    with pytest.raises(ComponentError, match="draws a visual"):
        check_sources(found)


def test_a_source_without_rules_fails():
    found = sources(only={"insights": []})
    with pytest.raises(ComponentError, match="has no insight rules"):
        check_sources(found)


def test_a_source_with_a_parameter_the_host_lacks_fails():
    host = _component(
        HOST.replace("params: {period: L6W}\n", "").replace("$period", "L6W"), id="host"
    )
    only = _component(ONLY.replace("$period", "$span"), id="only")
    with pytest.raises(ComponentError, match=r"needs the parameter\(s\) \$span"):
        check_sources({"only": only, "host": host})


def test_a_component_is_not_its_own_source():
    host = _component(HOST, id="host", insights_from=["host"])
    with pytest.raises(ComponentError, match="not its own source"):
        check_sources({"host": host})


def test_a_slot_binds_its_sources_with_its_parameters_and_filters(ctx):
    found = report("chart")
    # the packaged `period_kpis` is not on the tracer page: build a page for it
    raw = tracer_spec(
        pages=[
            {
                "id": "kpis",
                "title": "K",
                "params": {"period": "L6W"},
                "grid": {"columns": 12, "rows": [1]},
                "slots": [{"component": "period_kpis", "row": 1, "col": 1, "span": 12}],
            }
        ],
        filters={"where": {"payment_type": "Cash"}},
        outputs={"pdf": False},
        text_blocks={},
        deliver={},
    )
    raw["insights"] = "chart"
    found = parse_report(raw, "reports/k.yml", COMPONENTS)
    run = run_report(found, ctx, GENERATED)
    (slot,) = run.pages[0].slots
    names = [s.bound.id for s in slot.slot.sources]
    assert names == ["net_sales_drivers", "aov_drivers"]
    for source in slot.slot.sources:
        assert source.params["period"] == "L6W"
        assert [f.dimension for f in source.filters] == ["payment_type"]
    assert slot.status == "no_comparison"  # nothing to compare on the fixture
    assert slot.insights == []


# --- the room --------------------------------------------------------------------------------


def two_rows(insights: str):
    raw = tracer_spec(
        pages=[
            {
                "id": "p",
                "title": "P",
                "grid": {"columns": 12, "rows": [1, 1.8]},
                "slots": [
                    {"component": "category_contributive_aov", "row": 1, "col": 1, "span": 12},
                    {"component": "full_history_kpis", "row": 2, "col": 1, "span": 12},
                ],
            }
        ],
        outputs={"pdf": False},
        text_blocks={},
        deliver={},
    )
    raw["insights"] = insights
    return parse_report(raw, "reports/p.yml", COMPONENTS)


def test_check_size_keeps_two_lines_for_a_chart_with_rules_when_the_report_is_chart():
    two_rows("none")  # fits the table
    with pytest.raises(ReportError, match="the visual table needs at least"):
        two_rows("chart")


def test_check_size_keeps_nothing_for_a_chart_without_rules():
    rect = (0.035, 0.07, 0.9, 0.25)
    layouts.check_size(rect, "kpi_card", False, False, (1, 4), "slot")
    assert layouts.inner_size(rect, False, False, 2)[1] < layouts.inner_size(rect, False, False)[1]
    reserved = layouts.inner_size(rect, True, True)[1] - layouts.inner_size(rect, True, True, 2)[1]
    assert reserved == pytest.approx(visuals.foot_points(2) * visuals.POINT / 8.27)


def test_the_weekly_overview_keeps_room_for_the_key_figures_sentences():
    from hari_lari_analytics.reporting.reports import load_reports

    weekly = load_reports(COMPONENTS)["weekly"]
    overview = next(p for p in weekly.spec.plain_pages() if p.id == "overview")
    assert overview.grid.rows == [1.35, 4]
    details = next(p for p in weekly.spec.plain_pages() if p.id == "details")
    assert details.grid.rows[0] < 7  # row 2 keeps the room for two lines under its tables


# --- the bundle ------------------------------------------------------------------------------


def test_the_bundle_has_the_sentences_and_the_status_of_every_slot(ctx, tmp_path):
    built = build_report(report("chart"), ctx, tmp_path, GENERATED)
    bundle = json.loads(built.bundle.read_text())
    assert bundle["report"]["insights"] == "chart"
    slots = {s["component"]: s for s in bundle["pages"][0]["slots"]}
    table = slots["category_contributive_aov"]
    assert table["insights"] == [] and table["insights_status"] == "no_comparison"
    assert slots["full_history_kpis"]["insights_status"] is None


def test_the_bundle_carries_the_sentences_whatever_the_option_says(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "component_insights", lambda *a, **k: found(3, 0.5))
    built = build_report(report("none"), ctx, tmp_path, GENERATED)
    bundle = json.loads(built.bundle.read_text())
    assert bundle["report"]["insights"] == "none"
    slot = bundle["pages"][0]["slots"][1]
    assert [i["text"] for i in slot["insights"]] == ["0.5 0", "0.5 1"]
    assert slot["insights_status"] == "found"
