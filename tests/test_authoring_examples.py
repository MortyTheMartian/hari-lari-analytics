"""The two worked examples of the authoring guide (plan 8.8), run for real.

`.cursor/rules/reporting-components.mdc` and the README quote these snippets; the tests check that
the quotes are verbatim and that the examples work, so the guide cannot drift from the code.

1. A new metric and a new component, in YAML only (`refund_rate`, `refund_rate_by_payment_type`).
2. A new transform in Python (`min_value`) with its test, used by a component.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from PIL import Image

from hari_lari_analytics.analysis import transforms
from hari_lari_analytics.analysis.compiler import Query, run_query
from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.analysis.result import Result, ResultMeta
from hari_lari_analytics.analysis.transforms import (
    Options,
    Requirements,
    TransformContext,
    TransformError,
    apply,
    need_columns,
    transform,
)
from hari_lari_analytics.analysis.transforms.shape import row_masks
from hari_lari_analytics.presentation.builder import run_report
from hari_lari_analytics.presentation.components import ComponentError, parse_component
from hari_lari_analytics.presentation.facets import run_facets
from hari_lari_analytics.presentation.preview import render_preview
from hari_lari_analytics.presentation.reports import parse_report
from hari_lari_analytics.semantic import registry as registry_module
from hari_lari_analytics.semantic.registry import RegistryError, load_registry
from hari_lari_analytics.warehouse.db import connect

from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / ".cursor" / "rules" / "reporting-components.mdc"

# --- example 1: a metric and a component, YAML only ---------------------------------------------

REFUND_RATE = """\
refund_rate:
  label: Refund rate
  description: Refunds as a share of gross sales
  format: pct
  up_is_good: false
  additivity: none
  derived: refunds / nullif(gross_sales, 0)
"""

REFUND_COMPONENT = """\
# yaml-language-server: $schema=../schema/component.schema.json
id: refund_rate_by_payment_type
title: Refund rate by payment type
hint: Refunds as a share of gross sales
params: { period: L6W }
query:
  model: fct_sales_lines
  dimensions: [payment_type]
  metrics: [gross_sales, refunds, refund_rate]
  window: $period
  compare: prior
  totals: true
visual: { type: table, bars: [refund_rate], deltas: { refund_rate: delta } }
"""

REFUND_SLOT = """\
      - { component: refund_rate_by_payment_type, row: 2, col: 1, span: 6 }
"""

# --- example 2: a transform in Python -----------------------------------------------------------

MIN_VALUE_TRANSFORM = '''\
class MinValueOptions(Options):
    metric: str  # a metric of the query
    at_least: float


def _requires(options: MinValueOptions, registry: Registry) -> Requirements:
    return Requirements(metrics=(options.metric,))  # add the metric to the query if it lacks it


@transform("min_value", MinValueOptions, shapes=("rows",), requires=_requires)
def min_value(result: Result, options: MinValueOptions, ctx: TransformContext) -> Result:
    """Keep the rows whose metric reaches a value; the Other and total rows stay."""
    need_columns(result, "min_value", options.metric)
    regular, _, _ = row_masks(result.df)
    keep = ~regular | (result.df[options.metric] >= options.at_least)
    return result.replace(result.df[keep].reset_index(drop=True))
'''

MIN_VALUE_COMPONENT = """\
id: sizeable_items
title: Items with at least 40,000 IDR net sales
params: { period: full }
query:
  model: fct_sales_lines
  dimensions: [adjusted_item]
  metrics: [net_sales]
  window: $period
transforms:
  - min_value: { metric: net_sales, at_least: 40000 }
visual: { type: table, bars: [net_sales] }
"""

MIN_VALUE_TEST = """\
def test_min_value_keeps_the_big_rows_and_the_total(ctx):
    query = Query(
        model="fct_sales_lines",
        dimensions=["adjusted_item"],
        metrics=["net_sales"],
        window="full",
        totals=True,
    )
    result = run_query(ctx.con, query, ctx.registry, ctx.calendar)
    options = {"metric": "net_sales", "at_least": 40000}
    kept = apply("min_value", options, result, TransformContext(ctx.registry))
    items = kept.df[~kept.df["is_total"]]
    assert 0 < len(items) < (~result.df["is_total"]).sum()
    assert (items["net_sales"] >= 40000).all()
    assert kept.df["is_total"].sum() == 1  # the total row is kept as it was
"""


def snippet_in_rule(text: str) -> bool:
    return text.strip() in RULE.read_text()


@pytest.mark.parametrize(
    "snippet",
    [
        REFUND_RATE,
        REFUND_COMPONENT,
        REFUND_SLOT,
        MIN_VALUE_TRANSFORM,
        MIN_VALUE_COMPONENT,
        MIN_VALUE_TEST,
    ],
    ids=["metric", "component", "slot", "transform", "transform component", "transform test"],
)
def test_the_rule_quotes_the_example_verbatim(snippet):
    assert snippet_in_rule(snippet), "the guide and its tested example differ: copy it again"


@pytest.fixture(scope="module")
def packaged_metrics() -> str:
    return (Path(registry_module.__file__).parent / "metrics.yml").read_text()


@pytest.fixture(scope="module")
def registry(packaged_metrics):
    return load_registry(metrics=packaged_metrics + "\n" + REFUND_RATE)


@pytest.fixture(scope="module")
def ctx(registry):
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE), registry=registry)


# --- example 1 ---------------------------------------------------------------------------------


def component(registry, text=REFUND_COMPONENT, name="refund_rate_by_payment_type"):
    return parse_component(yaml.safe_load(text), f"components/{name}.yml", registry)


def test_without_the_metric_the_component_is_refused_with_a_message(packaged_metrics):
    with pytest.raises(ComponentError, match="refund_rate"):
        component(load_registry(metrics=packaged_metrics))


def test_the_metric_is_checked_when_the_registry_loads(packaged_metrics):
    with pytest.raises(RegistryError, match="refund_rate"):
        load_registry(metrics=packaged_metrics + REFUND_RATE.replace("refunds /", "refunz /"))


def test_the_component_runs_on_the_fixture(registry, ctx):
    (facet,) = run_facets(component(registry), ctx, {"period": "L14D"})
    df = facet.result.df
    by_type = df.set_index(df["payment_type"].fillna("Total"))
    assert by_type.loc["Gopay", "refund_rate"] == pytest.approx(1.0)  # the refunded R003
    assert by_type.loc["Total", "refund_rate"] == pytest.approx(40000 / 263000)
    assert facet.result.meta.metrics["refund_rate"].up_is_good is False


def test_the_component_draws_and_sits_in_a_report_slot(registry, ctx, tmp_path):
    comp = component(registry)
    overflows = render_preview(comp, ctx, {"period": "L14D"}, tmp_path / "refund.png")
    assert overflows == []
    with Image.open(tmp_path / "refund.png") as image:
        assert image.size[0] > 200
    report = parse_report(
        {
            "id": "example",
            "title": "Example",
            "pages": [
                {
                    "id": "refunds",
                    "title": "Refunds",
                    "grid": {"columns": 12, "rows": [1, 3]},
                    "slots": [
                        {"component": "refund_rate_by_payment_type", "row": 2, "col": 1, "span": 6}
                    ],
                }
            ],
        },
        "reports/example.yml",
        {comp.id: comp},
        registry,
    )
    run = run_report(report, ctx)
    assert run.pages[0].slots[0].facets[0].result.rows["payment_type"].tolist()[:1] == ["BCA"]


# --- example 2 ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def default_ctx():
    return QueryContext.create(connect(api_tables(), SEEDS, LAST_SALE))


@pytest.fixture
def min_value(monkeypatch):
    """Register the example transform for one test, in a copy of the registry."""
    monkeypatch.setattr(transforms, "TRANSFORMS", dict(transforms.TRANSFORMS))
    namespace = {
        "Options": Options,
        "Requirements": Requirements,
        "Registry": registry_module.Registry,
        "Result": Result,
        "TransformContext": TransformContext,
        "need_columns": need_columns,
        "row_masks": row_masks,
        "transform": transform,
    }
    exec(MIN_VALUE_TRANSFORM, namespace)  # the snippet of the guide
    return namespace["min_value"]


def test_the_transform_is_registered_by_its_decorator(min_value):
    spec = transforms.get("min_value")
    assert spec.shapes == ("rows",) and "at_least" in spec.options.model_fields
    assert spec.description.startswith("Keep the rows whose metric reaches a value")


def test_the_tests_of_the_guide_pass(min_value, default_ctx):
    namespace = {
        "Query": Query,
        "run_query": run_query,
        "apply": apply,
        "TransformContext": TransformContext,
    }
    exec(MIN_VALUE_TEST, namespace)  # the test of the guide, run as it is written
    namespace["test_min_value_keeps_the_big_rows_and_the_total"](default_ctx)


def test_the_transform_rejects_a_metric_the_result_lacks(min_value):
    meta = ResultMeta(shape="rows", window=None, empty=False)
    result = Result(pd.DataFrame({"adjusted_item": ["x"], "net_sales": [1.0]}), meta)
    with pytest.raises(TransformError, match="orders"):
        apply("min_value", {"metric": "orders", "at_least": 1}, result, TransformContext())


def test_the_component_with_the_transform_runs(min_value, default_ctx):
    comp = parse_component(
        yaml.safe_load(MIN_VALUE_COMPONENT),
        "components/sizeable_items.yml",
        default_ctx.registry,
    )
    (facet,) = run_facets(comp, default_ctx)
    items = facet.result.df[~facet.result.df["is_total"].astype(bool)]
    assert len(items) > 0 and (items["net_sales"] >= 40000).all()


def test_a_component_names_the_transform_it_lacks(default_ctx):
    with pytest.raises(ComponentError, match="min_value"):
        parse_component(
            yaml.safe_load(MIN_VALUE_COMPONENT),
            "components/sizeable_items.yml",
            default_ctx.registry,
        )
