"""The report files (plan 8.5): the loader and its errors, the expansion of repeats, the filters
and their precedence, schedules, anchors and the JSON Schema. No data is needed here."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from hari_lari_analytics.presentation import reports
from hari_lari_analytics.presentation.components import load_components
from hari_lari_analytics.presentation.reports import (
    ReportError,
    cron_problem,
    expand,
    load_report,
    load_reports,
    parse_report,
    resolve_anchor,
)

from .conftest import FIXTURES

COMPONENTS = load_components()
ANCHOR = date(2026, 10, 13)

TABLE = {"component": "category_contributive_aov", "row": 2, "col": 1, "span": 12}
KPIS = {"component": "full_history_kpis", "row": 1, "col": 1, "span": 12}


def page(page_id="overview", slots=None, **changes):
    return {
        "id": page_id,
        "title": "Overview",
        "grid": {"columns": 12, "rows": [1, 4]},
        "slots": slots if slots is not None else [KPIS, TABLE],
        **changes,
    }


def spec(**changes):
    return {
        "id": "t",
        "title": "Test",
        "params": {"period": "L14D"},
        "pages": [page()],
        **changes,
    }


def report(**changes):
    return parse_report(spec(**changes), "reports/t.yml", COMPONENTS)


def refuse(match, **changes):
    with pytest.raises(ReportError, match=match):
        report(**changes)


# --- the loader -----------------------------------------------------------------------------------


def test_the_tracer_report_loads():
    loaded = load_reports(COMPONENTS, FIXTURES / "reports")
    tracer = loaded["tracer"]
    assert tracer.spec.title == "Tracer report" and tracer.kicker == "Test café"
    assert [p.id for p in tracer.spec.plain_pages()] == ["overview"]


def test_a_missing_folder_has_no_reports():
    assert load_reports(COMPONENTS, FIXTURES / "nowhere") == {}


def test_kicker_and_footer_default_to_the_title_and_the_kicker():
    r = report()
    assert (r.kicker, r.footer) == ("Test", "Test")
    r = report(kicker="Café")
    assert (r.kicker, r.footer) == ("Café", "Café")


def test_the_report_gets_every_parameter_but_the_anchor():
    assert report(params={"period": "L14D", "anchor": "today"}).params == {"period": "L14D"}


def test_the_file_name_is_the_id(tmp_path):
    (tmp_path / "other.yml").write_text(yaml.safe_dump(spec()))
    with pytest.raises(
        ReportError, match=r"reports/other.yml: id: t must be the file name \(other\)"
    ):
        load_report(tmp_path / "other.yml", COMPONENTS)


def test_bad_yaml_and_non_mappings_are_refused(tmp_path):
    (tmp_path / "t.yml").write_text("id: [unclosed")
    with pytest.raises(ReportError, match="reports/t.yml: not valid YAML"):
        load_report(tmp_path / "t.yml", COMPONENTS)
    with pytest.raises(ReportError, match="expected a mapping"):
        parse_report(["x"], "reports/t.yml", COMPONENTS)


def test_unknown_keys_are_refused_with_their_location():
    refuse(r"pages.0.slots.0.spam: Extra inputs", pages=[page(slots=[{**KPIS, "spam": 1}])])
    refuse(r"report: |colour: Extra inputs", colour="red")


def test_a_report_needs_pages():
    refuse("pages", pages=[])


def test_an_unknown_component_names_the_known_ones():
    refuse(
        r"slot 1 \(nope\): no component 'nope' \(known: [a-z_, ]*category_contributive_aov, ",
        pages=[page(slots=[{**KPIS, "component": "nope"}])],
    )


def test_a_slot_too_small_for_its_visual_is_an_error_of_the_page():
    refuse(
        r"page overview: slot 1 \(category_contributive_aov\): the visual table needs at least",
        pages=[page(slots=[{**TABLE, "row": 1, "span": 2}])],
    )


def test_a_slot_outside_the_grid_is_an_error_of_the_page():
    refuse(r"page overview: slot 1 \(full_history_kpis\): col 10 and span 6", pages=[
        page(slots=[{**KPIS, "col": 10, "span": 6}])])  # fmt: skip


def test_a_component_without_visual_cannot_be_on_a_page():
    refuse(
        r"the component lines_export has no visual to draw",
        pages=[page(slots=[{**KPIS, "component": "lines_export"}])],
    )


def test_a_bad_parameter_is_an_error_of_the_slot():
    refuse(
        r"slot 1 \(category_contributive_aov\)",
        pages=[page(slots=[{**TABLE, "row": 1, "params": {"period": "FOREVER"}}])],
    )


def test_page_ids_must_be_unique():
    refuse(r"page id\(s\) used twice: overview", pages=[page(), page()])


def test_page_titles_are_templates():
    r = report(pages=[page(title="Week {{ anchor_iso_week }}, {{ period }}")])
    (one,) = expand(r, ANCHOR)
    assert one.title == "Week 2026-W42, L14D"
    refuse(r"title", pages=[page(title="{{ nope }}")])


# --- repeats --------------------------------------------------------------------------------------


def repeat(values, pages=None, **changes):
    return {"repeat": {"period": values}, "pages": pages or [page(title="{{ period }}")], **changes}


def test_a_repeat_over_periods_makes_one_page_per_value():
    r = report(pages=[repeat(["L14D", "L6W", "L3M"])])
    pages = expand(r, ANCHOR)
    assert [p.id for p in pages] == ["overview_l14d", "overview_l6w", "overview_l3m"]
    assert [p.title for p in pages] == ["L14D", "L6W", "L3M"]
    assert [p.period for p in pages] == ["L14D", "L6W", "L3M"]
    assert {p.base for p in pages} == {"overview"}
    assert pages[1].slots[1].ref.params["period"] == "L6W"  # bound for each value


def test_repeated_and_plain_pages_keep_their_order():
    r = report(pages=[page("cover"), repeat(["L14D", "L6W"])])
    assert [p.id for p in expand(r, ANCHOR)] == ["cover", "overview_l14d", "overview_l6w"]


def test_a_repeat_at_the_top_repeats_every_page():
    r = report(repeat={"period": ["L14D", "L6W"]}, pages=[page("a"), page("b")])
    assert [p.id for p in expand(r, ANCHOR)] == ["a_l14d", "b_l14d", "a_l6w", "b_l6w"]


def test_a_repeat_at_the_top_takes_plain_pages_only():
    r = report(pages=[repeat(["L14D"])])
    object.__setattr__(r.spec, "repeat", {"period": ["L6W"]})
    with pytest.raises(ReportError, match="list plain pages"):
        expand(r, ANCHOR)


def test_a_repeat_names_one_parameter_with_values():
    refuse(
        "repeat exactly one parameter", pages=[{**repeat(["L6W"]), "repeat": {"a": [1], "b": [2]}}]
    )
    refuse("snake_case", pages=[{**repeat(["L6W"]), "repeat": {"Period": ["L6W"]}}])
    refuse(r"repeat period: no values", pages=[repeat([])])
    refuse(r"repeat period: ", pages=[repeat(["L6W", "FOREVER"])])


def test_when_empty_works_for_periods_only():
    refuse(
        "when_empty works for a repeat over period, not over category",
        pages=[
            {
                "repeat": {"category": ["Coffee"]},
                "when_empty": {"message": "none"},
                "pages": [page(title="{{ category }}", params={"period": "L14D"})],
            }
        ],
    )


class EmptyIn(reports.Resolver):
    """A resolver for which some windows have no sales."""

    def __init__(self, *empty):
        self.empty = empty

    def is_empty(self, window, params):
        return window in self.empty


def test_an_empty_period_is_replaced_by_one_page_with_the_message():
    r = report(
        pages=[
            repeat(
                ["L14D", "L6W"],
                when_empty={"title": "Nothing in {{ period }}", "message": "No sales here"},
            )
        ]
    )
    pages = expand(r, ANCHOR, EmptyIn("L14D"))
    assert [p.id for p in pages] == ["empty_l14d", "overview_l6w"]
    assert pages[0].slots == [] and pages[0].empty_message == "No sales here"
    assert pages[0].title == "Nothing in L14D" and pages[0].period == "L14D"


def test_the_default_message_of_an_empty_period():
    r = report(pages=[repeat(["L14D"], when_empty={})])
    (one,) = expand(r, ANCHOR, EmptyIn("L14D"))
    assert (one.title, one.empty_message) == ("Overview", "No sales in this period")


def test_without_when_empty_an_empty_period_still_gets_its_pages():
    r = report(pages=[repeat(["L14D"])])
    assert [p.id for p in expand(r, ANCHOR, EmptyIn("L14D"))] == ["overview_l14d"]


def top(**changes):
    return {
        "dimension": "adjusted_category",
        "top": 5,
        "by": "net_sales",
        "window": "$period",
        **changes,
    }


def category_page():
    return page(
        "cat",
        slots=[{**KPIS, "ignore_filters": True}],
        params={},
        title="{{ category }}",
    )


def test_a_top_repeat_is_checked_with_a_placeholder_and_no_data():
    r = report(pages=[{"repeat": {"category": top()}, "pages": [category_page()]}])
    (one,) = expand(r, ANCHOR)
    assert one.id == "cat_" + reports.slug(f"{reports.PLACEHOLDER}category")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dimension": "nope"}, "repeat category: "),
        ({"by": "nope"}, "repeat category: "),
        ({"window": "FOREVER"}, "repeat category: "),
        ({"top": 0}, "top"),
    ],
)
def test_a_bad_top_repeat_is_refused(changes, message):
    refuse(message, pages=[{"repeat": {"category": top(**changes)}, "pages": [category_page()]}])


def test_the_model_of_a_top_repeat_is_the_first_that_has_the_dimension_and_metric():
    registry = report().registry
    assert reports.top_model(registry, reports.TopRepeat(**top())) == "fct_sales_lines"
    assert (
        reports.top_model(registry, reports.TopRepeat(**top(model="fct_orders", by="orders")))
        == "fct_orders"
    )
    with pytest.raises(Exception, match="no model has both"):
        reports.top_model(registry, reports.TopRepeat(**top(by="nope")))


def test_slug():
    assert reports.slug("L6W") == "l6w"
    assert reports.slug("Coffee & Tea") == "coffee_tea"
    assert reports.slug("§") == "x"


# --- filters --------------------------------------------------------------------------------------


def applied(r, index=1, which=0):
    one = expand(r, ANCHOR)[which]
    return {(f.kind, f.dimension): (f.origin, f.value) for f in one.slots[index].ref.filters}


def test_a_report_filter_applies_to_the_slots_whose_model_has_the_dimension():
    r = report(
        filters={"where": {"payment_type": ["Cash"]}, "focus": {"adjusted_category": ["Coffee"]}}
    )
    assert applied(r) == {
        ("where", "payment_type"): ("report", ["Cash"]),
        ("focus", "adjusted_category"): ("report", ["Coffee"]),
    }
    (one, *_) = expand(r, ANCHOR)
    assert one.slots[1].ref.with_["query"] == {
        "where": {"payment_type": ["Cash"]},
        "focus": {"adjusted_category": ["Coffee"]},
    }


def test_the_page_filter_replaces_the_report_filter_of_the_same_dimension():
    r = report(
        filters={"where": {"payment_type": ["Cash"]}},
        pages=[page("a"), page("b", filters={"where": {"payment_type": ["Card"]}})],
    )
    assert applied(r, which=0) == {("where", "payment_type"): ("report", ["Cash"])}
    assert applied(r, which=1) == {("where", "payment_type"): ("page", ["Card"])}


def test_the_slots_own_with_replaces_the_page_filter():
    own = {**TABLE, "with": {"query": {"where": {"payment_type": ["Other"]}}}}
    r = report(
        pages=[page(slots=[KPIS, own], filters={"where": {"payment_type": ["Card"]}})],
    )
    assert applied(r, index=0) == {("where", "payment_type"): ("page", ["Card"])}
    assert applied(r, index=1) == {}  # not applied: the slot's own filter wins
    (one, *_) = expand(r, ANCHOR)
    assert one.slots[1].ref.with_["query"]["where"] == {"payment_type": ["Other"]}


def test_a_filter_replaces_the_components_own_filter_of_the_same_dimension():
    # the component's own `where` is replaced, not merged: precedence component < report
    r = report(filters={"where": {"payment_type": ["Cash"]}})
    (one, *_) = expand(r, ANCHOR)
    assert one.slots[1].ref.bound.query.where["payment_type"] == ["Cash"]


def test_a_slot_opts_out_of_every_filter_or_of_some():
    filters = {"where": {"payment_type": ["Cash"]}, "focus": {"adjusted_category": ["Coffee"]}}
    off = {**TABLE, "ignore_filters": True}
    assert applied(report(filters=filters, pages=[page(slots=[KPIS, off])])) == {}
    some = {**TABLE, "ignore_filters": ["adjusted_category"]}
    r = report(filters=filters, pages=[page(slots=[KPIS, some])])
    assert list(applied(r)) == [("where", "payment_type")]


def test_a_facet_by_a_filtered_dimension_is_refused_until_the_slot_opts_out():
    facet = {"with": {"facet": {"by": "adjusted_category", "values": ["Coffee"]}}}
    refuse(
        r"the report's filter on adjusted_category would be replaced by the facet by "
        r"adjusted_category.*ignore_filters: \[adjusted_category\]",
        filters={"where": {"adjusted_category": ["Coffee"]}},
        pages=[page(slots=[KPIS, {**TABLE, **facet}])],
    )
    report(
        filters={"where": {"adjusted_category": ["Coffee"]}},
        pages=[page(slots=[KPIS, {**TABLE, **facet, "ignore_filters": ["adjusted_category"]}])],
    )


def test_a_filter_on_an_unknown_dimension_is_refused():
    refuse("the report: filters.where: unknown dimension nope", filters={"where": {"nope": ["x"]}})
    refuse(
        "page overview: filters.focus: unknown dimension nope",
        pages=[page(filters={"focus": {"nope": ["x"]}})],
    )


def test_a_filter_that_applies_to_no_slot_is_an_error():
    only_kpis = page(slots=[KPIS])
    refuse(
        r"filters.where.adjusted_category: applies to no slot of the report|"
        r"filters.where.adjusted_category",
        filters={"where": {"adjusted_category": ["Coffee"]}},
        pages=[{**only_kpis, "slots": [{**KPIS, "ignore_filters": True}]}],
    )
    refuse(
        r"page overview: filters.where.adjusted_category: applies to no slot of the page",
        pages=[page(filters={"where": {"adjusted_category": ["Coffee"]}},
                    slots=[{**KPIS, "ignore_filters": True}])],
    )  # fmt: skip


def test_the_page_chips_list_the_filters_in_force():
    r = report(
        filters={"where": {"payment_type": ["Cash"]}},
        pages=[page(filters={"focus": {"adjusted_category": ["Coffee"]}})],
    )
    (one, *_) = expand(r, ANCHOR)
    assert [(f.origin, f.kind, f.dimension) for f in one.filters] == [
        ("report", "where", "payment_type"),
        ("page", "focus", "adjusted_category"),
    ]


def test_the_page_chips_show_the_repeats_value():
    r = report(
        repeat={"category": ["Coffee", "Tea"]},
        filters={"focus": {"adjusted_category": "$category"}},
    )
    assert [p.filters[0].value for p in expand(r, ANCHOR)] == ["Coffee", "Tea"]


# --- outputs, text blocks and delivery ------------------------------------------------------------


def test_outputs_may_only_name_existing_pages_and_blocks():
    refuse(r"outputs.png: no page nope \(pages: overview\)", outputs={"png": ["nope"]})
    refuse("outputs.telegram: no text block nope", outputs={"telegram": ["nope"]})
    report(outputs={"png": "all", "pdf": True})


def test_a_text_line_may_only_use_known_names():
    refuse(
        r"text_blocks.b: nope is not available",
        text_blocks={"b": [{"text": "{{ nope }}"}]},
    )
    refuse("text_blocks.b: ", text_blocks={"b": [{"text": "{{ unclosed"}]})
    report(text_blocks={"b": [{"text": "{{ anchor }} {{ period }} {{ drive_url }}"}]})


def test_a_text_block_needs_a_visual_with_a_text_form():
    refuse(
        "has no visual: a text block needs a visual with a text form",
        text_blocks={"b": [{"component": "lines_export"}]},
    )


def test_a_text_block_is_not_empty_and_has_a_snake_case_name():
    refuse("needs at least one item", text_blocks={"b": []})
    refuse("snake_case", text_blocks={"B": [{"text": "x"}]})
    refuse(
        "insights: no page nope", text_blocks={"b": [{"insights": {"top": 2, "pages": ["nope"]}}]}
    )


def test_delivery_is_checked_against_the_outputs_and_the_targets():
    blocks = {
        "text_blocks": {"s": [{"text": "x"}]},
        "outputs": {"telegram": ["s"], "png": ["overview"]},
    }
    ok = {"to": "group", "photo": "overview", "text": "s"}
    report(**blocks, deliver={"telegram": [ok]})
    refuse(
        "no target 'nobody' in project.yml",
        **blocks,
        deliver={"telegram": [{**ok, "to": "nobody"}]},
    )
    refuse(
        "text: s is not listed in outputs.telegram",
        deliver={"telegram": [{"to": "group", "text": "s"}]},
    )
    refuse("photo: no page nope", **blocks, deliver={"telegram": [{**ok, "photo": "nope"}]})
    refuse(
        "photo: overview is not listed in outputs.png",
        text_blocks=blocks["text_blocks"],
        outputs={"telegram": ["s"]},
        deliver={"telegram": [ok]},
    )
    refuse(
        "document: pdf is not an output",
        **blocks,
        deliver={"telegram": [{**ok, "document": "pdf"}]},
    )
    refuse("send a photo, a document or a text", deliver={"telegram": [{"to": "group"}]})


def test_drive_files_must_be_outputs_and_the_folder_a_known_template():
    refuse("files: pdf is not an output", deliver={"drive": {"folder": "x", "files": ["pdf"]}})
    refuse("folder: nope is not available", outputs={"pdf": True},
           deliver={"drive": {"folder": "{{ nope }}", "files": ["pdf"]}})  # fmt: skip
    report(
        outputs={"pdf": True},
        deliver={"drive": {"folder": "{{ anchor }} {{ period }}", "files": ["pdf", "json"]}},
    )


def test_delivery_conditions_name_one_thing_that_exists():
    refuse(
        "exactly one of insight, rows and checks", deliver={"when": [{"insight": "x", "rows": "y"}]}
    )
    refuse("unknown insight rule nope", deliver={"when": [{"insight": "nope"}]})
    refuse("no component nope", deliver={"when": [{"rows": "nope"}]})
    report(deliver={"when": [{"checks": "warn"}, {"rows": "category_contributive_aov"}]})


def test_the_telegram_limit_is_a_caption_with_a_photo_or_document():
    caption = reports.TelegramDelivery(to="group", photo="overview")
    message = reports.TelegramDelivery(to="group", text="s")
    assert (caption.limit, message.limit) == (1024, 4096)
    assert reports.TelegramDelivery(to="group", document="pdf", text="s").limit == 1024


# --- schedules ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cron", ["0 7 * * 1", "*/15 6-9 * * mon-fri", "0 0 1,15 jan,jul *", "30 23 * * 7"]
)
def test_valid_cron_expressions(cron):
    assert cron_problem(cron) is None


@pytest.mark.parametrize(
    ("cron", "message"),
    [
        ("0 7 * *", "five fields"),
        ("60 7 * * *", "minute: '60' is outside 0-59"),
        ("0 24 * * *", "hour: '24' is outside 0-23"),
        ("0 0 0 * *", "day of month: '0' is outside 1-31"),
        ("0 0 * 13 *", "month: '13' is outside 1-12"),
        ("0 0 * * funday", "day of week: 'funday' is not valid"),
        ("*/0 * * * *", "'*/0' is not valid"),
        ("0 9-7 * * *", "outside 0-23"),
    ],
)
def test_bad_cron_expressions(cron, message):
    assert message in (cron_problem(cron) or "")
    refuse(message, schedule={"cron": cron})


def test_a_schedule_has_a_time_zone_that_exists():
    r = report(schedule={"cron": "0 7 * * 1", "timezone": "Asia/Makassar"})
    assert r.spec.schedule.enabled and r.spec.schedule.timezone == "Asia/Makassar"
    refuse(
        "'Mars/Base' is not a time zone", schedule={"cron": "0 7 * * 1", "timezone": "Mars/Base"}
    )
    assert report(schedule={"cron": "0 7 * * 1", "enabled": False}).spec.schedule.enabled is False


# --- the anchor -----------------------------------------------------------------------------------

NOW = datetime(2026, 11, 25, 1, 30, tzinfo=ZoneInfo("UTC"))  # 09:30 on the 25th in Makassar


def test_the_anchor_comes_from_the_flag_then_the_report_then_the_project():
    assert resolve_anchor(report(params={"anchor": "2026-01-02"}), date(2026, 3, 4)) == date(
        2026, 3, 4
    )
    assert resolve_anchor(report(params={"anchor": "2026-01-02"})) == date(2026, 1, 2)
    assert resolve_anchor(report(params={"anchor": "today"}), now=NOW) == date(2026, 11, 25)
    assert resolve_anchor(report(params={"anchor": "yesterday"}), now=NOW) == date(2026, 11, 24)
    assert resolve_anchor(report(), now=NOW) == date(2026, 11, 24)  # the rule of project.yml


def test_a_bad_anchor_is_refused():
    refuse("params.anchor: 'soon' is not yesterday, today or a date", params={"anchor": "soon"})


# --- the JSON Schema ------------------------------------------------------------------------------


def test_the_report_schema_is_up_to_date_and_lists_components_and_dimensions():
    packaged = sorted(COMPONENTS)
    assert reports.schema_path().read_text() == reports.schema_text(packaged)
    schema = json.loads(reports.schema_text(packaged))
    slot = schema["$defs"]["SlotSpec"]["properties"]["component"]
    assert slot["enum"] == packaged
    where = schema["$defs"]["FilterSpec"]["properties"]["where"]["propertyNames"]["enum"]
    assert "adjusted_category" in where and "payment_type" in where


def test_the_schema_accepts_the_tracer_and_refuses_a_bad_slot():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(reports.schema_text(sorted(COMPONENTS)))
    tracer = yaml.safe_load((FIXTURES / "reports" / "tracer.yml").read_text())
    jsonschema.validate(tracer, schema)
    bad = copy.deepcopy(tracer)
    bad["pages"][0]["slots"][0]["component"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
