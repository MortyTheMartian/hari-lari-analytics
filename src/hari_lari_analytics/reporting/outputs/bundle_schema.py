"""The schema of `bundle.json` (plan 8.9): the contract between a report build and any renderer.

The models below describe the file exactly (an unknown key is an error), so the JSON Schema
`schema/bundle.schema.json` is generated from them like the schemas of components and reports,
`hari-lari-report schema --check` fails when the file is out of date, and a test validates every
bundle the builder writes against it. A frontend (phase 12) reads this file and never computes:

- `report`: the report, the day it ends on, when it was built and from which extraction run;
- `theme`: the colour of every token and the series colours, as hex, so a renderer draws the
  tokens in the visuals' options in the same colours as the PDF;
- `pages`: each with its chips, note, 12-column grid and `slots`; a slot names its component,
  rendered title and hint, visual with options, position, filters and `results` (the distinct
  results of its facets, each as columns and rows of display cells) and `facets`, which point
  at the results;
- the page's `insights`, and `texts`, the Telegram text blocks (the Drive link is still the
  placeholder `{{ drive_url }}`).

A cell is `{value, text, ...}`: the raw value to draw with and the string to show. A renderer
shows `text`, colours by `tone` unless `small`, and never formats a number itself. Nothing in the
bundle comes from a free-text field of the sales lines (customers, notes).
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hari_lari_analytics.reporting.formats import Tone
from hari_lari_analytics.reporting.present import ColumnKind, RowKind
from hari_lari_analytics.reporting.registry import Format
from hari_lari_analytics.reporting.result import Shape
from hari_lari_analytics.reporting.visuals.theme import Token
from hari_lari_analytics.reporting.windows import Compare, Grain

FORMAT_VERSION = 1
SCHEMA_ID = "https://hari-lari.example/schema/bundle.schema.json"

Scalar = bool | int | float | str | None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- results --------------------------------------------------------------------------------------


class Cell(_Strict):
    value: Scalar = Field(description="the raw value, to draw with; null when missing")
    text: str = Field(description="the display string: IDR, compact, percent, `n/a` or `–`")
    compact: str | None = Field(default=None, description="the short form, when it differs")
    tone: Tone | None = Field(
        default=None, description="the tone of a change, delta or growth; absent for a value"
    )
    small: bool | None = Field(
        default=None, description="below the sample-size rule: draw it grey, never as a signal"
    )
    n: int | None = Field(default=None, description="the count of the rule, for `n=12`")


class Column(_Strict):
    id: str
    kind: ColumnKind
    label: str
    metric: str | None = None
    format: Format | None = None
    unit: str | None = None


class Row(_Strict):
    label: str
    kind: RowKind
    small: bool | None = None
    n: int | None = None
    cells: dict[str, Cell] = Field(description="by column id")


class WindowRange(_Strict):
    start: str = Field(description="first day, ISO date")
    end: str = Field(description="last day, ISO date, inclusive")
    title: str
    label: str
    days: int
    trading_days: int
    grain: Grain


class ResultWindow(_Strict):
    key: str = Field(description="the window as written: L14D, WTD, full, ...")
    current: WindowRange
    compare: Compare
    compare_ok: bool = Field(description="false: the comparison is not valid; show `n/a`")
    compare_label: str | None = None
    comparison: WindowRange | None = None


class MetricInfo(_Strict):
    label: str
    description: str
    format: Format
    unit: str
    up_is_good: bool | None = Field(description="null: neutral, the change is not coloured")


class Event(_Strict):
    date: str
    kind: str = Field(description="holiday, closure or promotion")
    name: str


class BucketInfo(_Strict):
    ordinal: int
    start: str
    end: str


class DecompositionInfo(_Strict):
    identity: str
    kind: str
    kpi: str


class Result(_Strict):
    shape: Shape
    empty: bool
    window: ResultWindow | None = Field(
        default=None, description="absent for a result of a Python source"
    )
    trading_days: int | None = None
    prior_trading_days: int | None = None
    grain: Grain | None = None
    dimensions: dict[str, str] | None = Field(default=None, description="id to label")
    metrics: dict[str, MetricInfo] | None = None
    events: list[Event] | None = None
    buckets: list[BucketInfo] | None = None
    decomposition: DecompositionInfo | None = None
    columns: list[Column]
    rows: list[Row]


# --- pages ----------------------------------------------------------------------------------------


class VisualInfo(_Strict):
    type: str
    options: dict[str, Any] = Field(description="what to show, as in the component's YAML")


class Position(_Strict):
    row: int
    col: int
    span: int
    height: int


class Filter(_Strict):
    kind: Literal["where", "focus"]
    dimension: str
    origin: Literal["report", "page"]
    value: Any


class FacetRef(_Strict):
    key: Scalar
    label: str | None
    metric: str | None
    result: int = Field(description="the index in the slot's `results`")


class Slot(_Strict):
    component: str
    title: str
    hint: str
    visual: VisualInfo
    layout: str = Field(description="how the facets are laid out: row, column or grid(RxC)")
    position: Position
    card: bool = Field(description="false: the visual draws its own card")
    filters: list[Filter]
    results: list[Result]
    facets: list[FacetRef]


class Chip(_Strict):
    text: str
    style: Literal["fill", "line", "muted"]


class Grid(_Strict):
    columns: Literal[12]
    rows: list[float] = Field(description="the row heights, in parts")


class Insight(_Strict):
    text: str
    tone: Tone
    rule: str
    score: float
    component: str | None = None
    facet: str | None = None


class Page(_Strict):
    id: str
    base: str = Field(description="the page's id in the YAML; a repeat has a longer `id`")
    title: str
    params: dict[str, Any]
    period: str | None
    chips: list[Chip]
    note: str
    empty_message: str | None = Field(
        description="the `when_empty` message: the page has no slots and shows only this"
    )
    grid: Grid
    slots: list[Slot]
    insights: list[Insight]


# --- the file -------------------------------------------------------------------------------------


class ReportInfo(_Strict):
    id: str
    title: str
    kicker: str
    footer: str
    anchor: str = Field(description="the last day of the report, ISO date")
    generated: str = Field(description="when it was built, ISO date and time, local clock")
    run: str | None = Field(description="the extraction run it was built from")
    params: dict[str, Any]


class ThemeInfo(_Strict):
    font: str
    colors: dict[Token, str] = Field(description="every colour token as #rrggbb")
    series: list[str] = Field(description="the colours of the series, such as payment types")


class Bundle(_Strict):
    format_version: Literal[1]
    report: ReportInfo
    theme: ThemeInfo
    pages: list[Page] = Field(min_length=1)
    texts: dict[str, str]


# --- the JSON Schema file -------------------------------------------------------------------------


def bundle_json_schema() -> dict:
    schema = Bundle.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
        "title": "Hari Lari report bundle (bundle.json)",
        "description": (__doc__ or "").split("\n\n")[0],
    }


def schema_path() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "schema")) / (
        "bundle.schema.json"
    )


def schema_text() -> str:
    return json.dumps(bundle_json_schema(), indent=2) + "\n"


def write_schema(path: Path | None = None) -> Path:
    path = path or schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_text())
    return path


def check_bundle(raw: Any) -> Bundle:
    """The bundle as a model; a ValidationError names the field that breaks the contract."""
    return Bundle.model_validate(raw)
