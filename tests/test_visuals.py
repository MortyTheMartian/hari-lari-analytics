"""The visuals (plan 8.1 and 8.2): the registry and the loader's check of a component's visual,
and every visual drawn on the fixture, with the small-sample and calendar-event marks."""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import matplotlib
import pytest
import yaml
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.lines import Line2D
from matplotlib.text import Text

from hari_lari_analytics.reporting import visuals
from hari_lari_analytics.reporting.components import (
    ComponentError,
    load_components,
    parse_component,
)
from hari_lari_analytics.reporting.db import connect
from hari_lari_analytics.reporting.facets import run_facets
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME, TOKENS

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

FIXTURES = Path(__file__).parent / "fixtures" / "components"


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


@pytest.fixture(autouse=True)
def theme_rc():
    with matplotlib.rc_context(DEFAULT_THEME.rc()):
        yield


def component(text: str, name: str = "example"):
    raw = {"id": name, "title": name.replace("_", " "), **yaml.safe_load(text)}
    return parse_component(raw, f"components/{name}.yml")


def render(comp, ctx, params=None, insights=()):
    """Draw a component's facets into a card on an A4 page; return the page."""
    facets = run_facets(comp, ctx, params)
    spec = comp.bind(params).spec
    fig = visuals.page()
    area = visuals.card(visuals.Area(fig, (0.035, 0.07, 0.93, 0.78)), comp.id, "hint")
    layout = spec.facet.layout if spec.facet else "row"
    visuals.draw_facets(spec.visual.type, spec.visual.options, area, facets, layout, insights)
    FigureCanvasAgg(fig).print_png(io.BytesIO())  # draws every artist
    return fig


def texts(fig) -> list[Text]:
    return [t for t in fig.findobj(Text) if t.get_text()]


def strings(fig) -> list[str]:
    return [t.get_text() for t in texts(fig)]


# --- every visual on the fixture ------------------------------------------------------------

CASES = {
    "kpi_card": """
query: {model: fct_sales_lines, metrics: [net_sales, orders, aov], window: L14D, compare: prior}
facet: {by: metric, layout: grid(1x3)}
visual: {type: kpi_card}
""",
    "kpi_card_series": """
query: {model: fct_orders, dimensions: [bucket], metrics: [net_sales], window: L14D,
        totals: true}
visual: {type: kpi_card, sparkline: true}
""",
    "summary_card": """
params: {period: L14D}
query: {model: fct_orders, metrics: [net_sales, orders], window: $period, compare: prior}
facet: {by: period, values: [WTD, L14D, full], layout: row}
visual: {type: summary_card}
""",
    "text_line": """
query: {model: fct_orders, metrics: [net_sales, orders], window: L14D}
visual: {type: text_line}
""",
    "table": """
query: {model: fct_sales_lines, dimensions: [adjusted_category],
        metrics: [net_sales, share, orders, penetration], window: L14D, compare: prior,
        totals: true}
visual: {type: table, bars: [share], deltas: {penetration: delta}, prior: [net_sales],
         bold: [net_sales], rank: true}
""",
    "table_metrics": """
query: {model: fct_sales_lines, metrics: [net_sales, orders, aov], window: L14D, compare: prior}
visual: {type: table}
""",
    "bar_trend": """
query: {model: fct_orders, dimensions: [bucket], metrics: [net_sales, orders], window: L14D,
        compare: prior}
transforms: [growth_vs_previous]
visual: {type: bar_trend, metric: net_sales, line: orders, highlight: {top: 2}}
""",
    "hbar": """
query: {model: fct_sales_lines, dimensions: [adjusted_category], metrics: [net_sales],
        window: L14D, compare: prior}
visual: {type: hbar, highlight: {top: 1}}
""",
    "dot_plot": """
query: {model: fct_sales_lines, dimensions: [adjusted_category], metrics: [penetration],
        window: L14D, compare: prior}
visual: {type: dot_plot}
""",
    "stacked_share_bar": """
query: {model: fct_orders, dimensions: [payment_type], metrics: [share, orders], window: L14D}
visual: {type: stacked_share_bar, details: [orders]}
""",
    "heatmap": """
query: {model: fct_orders, dimensions: [weekday, hour_of_day], metrics: [orders], window: L14D}
transforms: [per_trading_day, {pivot: {rows: weekday, columns: hour_of_day}}]
visual: {type: heatmap}
""",
    "scatter_quadrant": """
query: {model: fct_sales_lines, dimensions: [adjusted_item], metrics: [items_sold, avg_item_price],
        window: L14D}
transforms: [{classify: {mode: quadrants, x: items_sold, y: avg_item_price}}]
visual: {type: scatter_quadrant, x: items_sold, y: avg_item_price}
""",
    "waterfall": """
query: {model: fct_orders, metrics: [aov], window: L14D, compare: prior}
transforms: [{decompose: {identity: aov_basket}}]
visual: {type: waterfall}
""",
    "notes_card": """
query: {source: notes}
visual: {type: notes_card, column: note, before: kind, empty: No notes}
""",
    "insights": """
query: {model: fct_orders, metrics: [net_sales], window: L14D}
visual: {type: insights}
""",
}


def test_every_visual_has_a_case():
    assert {yaml.safe_load(c)["visual"]["type"] for c in CASES.values()} == set(visuals.VISUALS)


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_visual_draws_on_the_fixture(name, ctx):
    fig = render(component(CASES[name], name), ctx)
    assert texts(fig), "the visual drew nothing"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.yml")), ids=lambda p: p.stem)
def test_the_fixture_components_draw(path, ctx):
    comp = load_components(FIXTURES)[path.stem]
    params = {"category": "Coffee"} if path.stem == "category_trend" else None
    render(comp, ctx, params)


def test_the_packaged_components_draw(ctx):
    for comp in load_components().values():
        render(comp, ctx)


# --- what the visuals show ------------------------------------------------------------------


def test_small_rows_are_grey_with_their_n(ctx):
    fig = render(component(CASES["table"], "table"), ctx)
    muted = DEFAULT_THEME.color("muted")
    small = [t for t in texts(fig) if "n=" in t.get_text()]
    assert small, "no row carries its n"
    assert all(t.get_color() == muted for t in small)


def with_comparison(result, n: int):
    """The result with a valid comparison: orders up 50% on every row, `n` orders on each side
    (the fixture has no valid comparison)."""
    df = result.df.copy()
    df["prior_orders"] = df["orders"] / 1.5
    df["orders_change"] = 0.5
    df["orders_delta"] = df["orders"] - df["prior_orders"]
    for c in ("n_orders", "prior_n_orders"):
        df[c] = n
    window = dataclasses.replace(result.meta.window, compare_ok=True)
    return result.replace(df, window=window)


@pytest.mark.parametrize(("n", "coloured"), [(5, False), (500, True)])
def test_a_change_is_coloured_unless_its_sample_is_small(ctx, n, coloured):
    comp = component(CASES["table"], "table")
    result = with_comparison(run_facets(comp, ctx)[0].result, n)
    fig = visuals.page()
    options = {"columns": ["orders"], "deltas": {"orders": "change"}}
    visuals.draw("table", options, visuals.Area(fig, (0.05, 0.05, 0.9, 0.9)), result)
    changes = [t for t in texts(fig) if t.get_text() == "+50.0%"]
    assert changes
    good = DEFAULT_THEME.color("good")
    assert all((t.get_color() == good) is coloured for t in changes)


def test_calendar_events_are_on_the_meta_and_marked(ctx):
    comp = component(CASES["bar_trend"], "bar_trend")
    result = run_facets(comp, ctx)[0].result
    kinds = {e.kind for e in result.meta.events}
    assert {"holiday", "closure", "promotion"} <= kinds
    fig = render(comp, ctx)
    marks = [m for m in fig.findobj(Line2D) if m.get_marker() == "^"]
    assert {m.get_color() for m in marks} >= {
        DEFAULT_THEME.color("holiday"),
        DEFAULT_THEME.color("promotion"),
    }
    assert {"holiday", "promotion"} <= set(strings(fig))


def test_a_query_without_time_has_no_events(ctx):
    result = run_facets(component(CASES["hbar"], "hbar"), ctx)[0].result
    assert result.meta.events == ()


def test_the_waterfall_says_when_there_is_no_valid_comparison(ctx):
    fig = render(component(CASES["waterfall"], "waterfall"), ctx)
    assert any(s.startswith("No valid comparison") for s in strings(fig))


def test_an_empty_window_says_no_sales(ctx):
    text = CASES["hbar"].replace("window: L14D", "window: L14D, where: {adjusted_category: Nope}")
    fig = render(component(text, "hbar"), ctx)
    assert visuals.EMPTY_TEXT in strings(fig)


def test_the_table_ranks_and_ends_with_the_total(ctx):
    fig = render(component(CASES["table"], "table"), ctx)
    labels = strings(fig)
    assert any(s.lstrip().startswith("1  ") for s in labels)
    assert "Total" in labels


def test_the_insights_visual_draws_the_sentences(ctx):
    class Sentence:
        def __init__(self, text, tone):
            self.text, self.tone = text, tone

    said = [Sentence("Orders rose 12% on the prior week.", "good"), Sentence("Quiet.", None)]
    fig = render(component(CASES["insights"], "insights"), ctx, insights=said)
    drawn = strings(fig)
    assert said[0].text in drawn
    dots = [t for t in texts(fig) if t.get_text() == "●"]
    assert dots[0].get_color() == DEFAULT_THEME.color("good")


# --- the plain-text forms -------------------------------------------------------------------


def test_text_forms(ctx):
    kpi = run_facets(component(CASES["kpi_card"], "kpi"), ctx)[0]
    line = visuals.plain_text("kpi_card", {}, kpi.result, visuals.DrawContext(metric="net_sales"))
    assert line[0].startswith("Net Sales: ")
    result = run_facets(component(CASES["text_line"], "line"), ctx)[0].result
    (text,) = visuals.plain_text("text_line", {}, result)
    assert " · Orders " in text
    notes = run_facets(component(CASES["notes_card"], "notes"), ctx)[0].result
    options = {"column": "note", "empty": "No notes"}
    assert visuals.plain_text("notes_card", options, notes) == ["No notes"]
    with pytest.raises(visuals.VisualError, match="no text form"):
        visuals.plain_text("heatmap", {}, result)


# --- the registry and the loader's check ----------------------------------------------------


@pytest.mark.parametrize(
    ("visual", "match"),
    [
        ("{type: chart}", "unknown visual 'chart'"),
        ("{type: table, fontsize: 8}", "fontsize: Extra inputs are not permitted"),
        ("{type: table, bars: [aov]}", "bars: aov is not a metric of the query"),
        ("{type: table, columns: [orders], bold: [net_sales]}", "net_sales is styled but not in"),
        ("{type: table, deltas: {orders: delta}}", "deltas needs a comparison"),
        ("{type: heatmap}", "does not draw a result of shape rows"),
        ("{type: bar_trend, color: '#ff0000'}", "color: Input should be"),
        ("{type: stacked_share_bar}", "must be a share"),
        ("{type: hbar, metric: aov}", "metric: aov is not a metric of the query"),
        ("{type: kpi_card}", "does not draw a result of shape rows"),
    ],
)
def test_the_loader_checks_the_visual(visual, match):
    text = (
        "query: {model: fct_orders, dimensions: [payment_type], metrics: [net_sales, orders],"
        f" window: L6W}}\nvisual: {visual}"
    )
    with pytest.raises(ComponentError, match=match) as error:
        component(text)
    assert str(error.value).startswith("components/example.yml: visual: ")


def test_a_kpi_card_on_a_series_needs_totals():
    text = (
        "query: {model: fct_orders, dimensions: [bucket], metrics: [orders], window: L6W}\n"
        "visual: {type: kpi_card}"
    )
    with pytest.raises(ComponentError, match="totals: true"):
        component(text)


def test_a_visual_of_a_python_source_is_checked_for_its_options():
    with pytest.raises(ComponentError, match="max_items"):
        component("query: {source: notes}\nvisual: {type: notes_card, column: note, max_items: 0}")
    component("query: {source: notes}\nvisual: {type: notes_card, column: anything}")


SIZE_WORDS = ("size", "font", "width", "height", "dpi", "px", "pt", "inch")


def _properties(schema: dict, defs: dict):
    for name, prop in schema.get("properties", {}).items():
        yield name, prop
        ref = prop.get("$ref") or next(
            (a.get("$ref") for a in prop.get("anyOf", []) if "$ref" in a), None
        )
        if ref:
            yield from _properties(defs[ref.rsplit("/", 1)[1]], defs)


@pytest.mark.parametrize("name", sorted(visuals.VISUALS))
def test_options_say_what_to_show_never_how(name):
    """No option is a size or a colour value: colours are theme tokens (docs/components.md)."""
    schema = visuals.get(name).options.model_json_schema()
    defs = schema.get("$defs", {})
    for prop_name, prop in _properties(schema, defs):
        assert not any(w in prop_name.split("_") for w in SIZE_WORDS), prop_name
        enum = prop.get("enum")
        if prop_name == "color":
            assert enum and set(enum) <= set(TOKENS), prop_name
        assert not str(prop.get("default", "")).startswith("#"), prop_name


def test_the_registry_describes_every_visual():
    for spec in visuals.VISUALS.values():
        assert spec.description, spec.name
        assert spec.accepts, spec.name
        w, h = spec.min_size
        assert 0 < w <= 1 and 0 < h <= 1
