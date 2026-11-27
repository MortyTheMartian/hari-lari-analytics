"""The display rules (plan 8.1): `formats.py` and the presentation step `present.py`, the same
strings for the PDF and the JSON bundle."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest
import yaml

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.presentation import formats
from hari_lari_analytics.presentation.components import parse_component
from hari_lari_analytics.presentation.facets import run_facets
from hari_lari_analytics.presentation.formats import DASH, NA
from hari_lari_analytics.presentation.present import TOTAL_LABEL, present
from hari_lari_analytics.semantic.registry import default_registry
from hari_lari_analytics.warehouse.db import connect

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS


@pytest.fixture(scope="module")
def ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


def run(ctx, text: str):
    raw = {"id": "example", "title": "Example", **yaml.safe_load(text)}
    return run_facets(parse_component(raw, "components/example.yml"), ctx)[0].result


def metric(metric_id: str):
    return default_registry().metric(metric_id)


# --- formats ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "fmt", "style", "text"),
    [
        (1234567, "idr", "full", "1,234,567"),
        (1234567, "idr", "compact", "1.23M"),
        (85000, "count", "compact", "85k"),
        (0.532, "pct", "full", "53.2%"),
        (2.0466, "decimal", "full", "2.05"),
        (None, "idr", "full", DASH),
        (float("nan"), "count", "full", DASH),
    ],
)
def test_values_by_format(value, fmt, style, text):
    assert formats.value_text(value, fmt, style) == text


def test_changes_deltas_and_points():
    assert formats.ratio_text(0.1234) == "+12.3%"
    assert formats.ratio_text(-0.04) == "-4.0%"
    assert formats.ratio_text(12.0) == ">+999%"
    assert formats.points_text(0.012) == "+1.2 pp"
    assert formats.delta_text(0.012, "pct") == "+1.2 pp"
    assert formats.delta_text(3812, "count") == "+3,812"
    assert formats.delta_text(-0.5, "decimal") == "-0.50"
    assert formats.delta_text(0, "count") == "±0"
    assert formats.ratio_text(None) == DASH


def test_a_pct_metric_changes_in_points_others_in_percent():
    assert formats.default_change(metric("penetration")) == "delta"
    assert formats.default_change(metric("net_sales")) == "change"
    assert formats.comparison_text(0.05, metric("penetration"), "delta") == "+5.0 pp"
    assert formats.comparison_text(0.05, metric("net_sales"), "change") == "+5.0%"


def test_tone():
    assert formats.tone(0.1, True) == "good"
    assert formats.tone(0.1, False) == "bad"
    assert formats.tone(-0.1, False) == "good"
    assert formats.tone(0.1, None) == "neutral"
    assert formats.tone(0, True) == "neutral"
    assert formats.tone(None, True) is None


@pytest.mark.parametrize(
    ("start", "grain", "tick"),
    [
        (date(2026, 11, 2), "day", "Mon\n02"),
        (date(2026, 11, 2), "week", "W45\n02 Nov"),
        (date(2026, 11, 1), "month", "Nov\n2026"),
    ],
)
def test_bucket_ticks(start, grain, tick):
    assert formats.bucket_tick(start, grain) == tick


# --- the presentation step -------------------------------------------------------------------


def test_a_single_result_is_one_total_row(ctx):
    result = run(ctx, "query: {model: fct_orders, metrics: [net_sales, aov], window: L14D}")
    pres = present(result)
    assert [r.kind for r in pres.rows] == ["total"]
    total = pres.total
    assert total.label == TOTAL_LABEL
    assert total["net_sales"].text == formats.value_text(total["net_sales"].value, "idr")
    assert pres.columns["net_sales"].unit == "IDR"


def test_small_rows_carry_their_n(ctx):
    result = run(
        ctx,
        "query: {model: fct_sales_lines, dimensions: [adjusted_category], metrics: [net_sales],"
        " window: L14D, totals: true}",
    )
    pres = present(result)
    assert all(r.small and r.n is not None for r in pres.regular)
    first = pres.regular[0]
    assert first["net_sales"].small and first["net_sales"].n == first.n
    assert first.label == first["adjusted_category"].text


def test_comparisons_are_na_when_not_valid(ctx):
    result = run(
        ctx, "query: {model: fct_orders, metrics: [net_sales], window: L14D, compare: prior}"
    )
    assert not result.meta.compare_ok
    row = present(result).total
    assert row["prior_net_sales"].text == NA
    assert row["net_sales_change"].text == NA
    assert row["net_sales_change"].tone is None


def test_a_valid_comparison_has_its_text_and_tone(ctx):
    result = run(ctx, "query: {model: fct_orders, metrics: [orders], window: L14D, compare: prior}")
    df = result.df.assign(prior_orders=2, orders_change=-0.25, orders_delta=-1)
    result = result.replace(df, window=dataclasses.replace(result.meta.window, compare_ok=True))
    row = present(result).total
    assert (row["orders_change"].text, row["orders_change"].tone) == ("-25.0%", "bad")
    assert row["orders_delta"].text == "-1"
    assert row["prior_orders"].text == "2"


def test_the_total_of_a_series_is_the_window_total(ctx):
    result = run(
        ctx,
        "query: {model: fct_orders, dimensions: [bucket], metrics: [orders], window: L14D,"
        " totals: true}",
    )
    pres = present(result)
    assert pres.total.label == TOTAL_LABEL
    assert pres.total["orders"].value == result.total["orders"]


def test_a_decomposition_formats_each_row_in_its_metric(ctx):
    result = run(
        ctx,
        "query: {model: fct_orders, metrics: [aov], window: L14D, compare: prior}\n"
        "transforms: [{decompose: {identity: aov_basket}}]",
    )
    pres = present(result)
    assert pres.total.kind == "total"
    assert pres.columns["effect"].unit == "IDR"
    for row in pres.rows:
        fmt = result.meta.metrics[result.df["metric"].iloc[row.position]].format
        assert row["current"].text == formats.value_text(row["current"].value, fmt)
        assert row["effect"].text == NA  # the fixture has no valid comparison


def test_a_matrix_is_labelled_by_its_dimensions(ctx):
    result = run(
        ctx,
        "query: {model: fct_orders, dimensions: [weekday, hour_of_day], metrics: [orders],"
        " window: L14D}\ntransforms: [{pivot: {rows: weekday, columns: hour_of_day}}]",
    )
    pres = present(result)
    hours = [c for c in pres.columns.values() if c.kind == "value"]
    assert [c.id for c in hours] == sorted(c.id for c in hours)
    assert pres.rows[0].label in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def test_rows_of_a_source_are_not_totals(ctx):
    result = run(ctx, "query: {source: notes}")
    assert all(r.kind == "row" and r.label != TOTAL_LABEL for r in present(result).rows)
