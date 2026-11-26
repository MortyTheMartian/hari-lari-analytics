"""The JSON results bundle (plan 8.9): everything a renderer needs, and no computation left.

`bundle.json` sits next to the PDF in `reports/<report>/<anchor>/`. It holds the report, its
pages, grid and slots; per facet the result as display cells (raw value, display string, tone,
small-sample flag, `n`) from the same presentation step as the PDF (`present.py`), plus the
metadata (windows, `compare_ok`, trading days, calendar events); the insights; the text blocks;
and a format version. It holds no SQL, no line notes and no Python-source results that carry
free text from the lines: only what the slots of the pages show.

A frontend (phase 12) renders this file and never formats a number or applies a rule itself.
`bundle_schema.py` describes the file; the JSON Schema `schema/bundle.schema.json` and the test
that validates every bundle against it are generated from there.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from hari_lari_analytics.reporting import formats
from hari_lari_analytics.reporting.builder import PageRun, ReportRun, SlotRun
from hari_lari_analytics.reporting.facets import Facet
from hari_lari_analytics.reporting.insights import Insight
from hari_lari_analytics.reporting.outputs.bundle_schema import FORMAT_VERSION
from hari_lari_analytics.reporting.present import Cell, Presentation, plain, present
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME, TOKENS, Theme
from hari_lari_analytics.reporting.windows import Window


def _window(window: Window | None) -> dict | None:
    if window is None:
        return None
    return {
        "start": window.start,
        "end": window.end,
        "title": window.title,
        "label": window.label,
        "days": window.days,
        "trading_days": window.trading_days,
        "grain": window.grain,
    }


def _cell(cell: Cell, compact: Cell | None = None) -> dict:
    out: dict[str, Any] = {"value": plain(cell.value), "text": cell.text}
    if compact is not None and compact.text != cell.text:
        out["compact"] = compact.text
    if cell.tone is not None:
        out["tone"] = cell.tone
    if cell.small:
        out["small"] = True
    if cell.n is not None:
        out["n"] = cell.n
    return out


def _presented(result: Result) -> dict:
    full: Presentation = present(result, "full")
    short: Presentation = present(result, "compact")
    return {
        "columns": [
            {
                "id": str(c.id),  # the hours of a matrix are numbers; a JSON key is text
                "kind": c.kind,
                "label": str(c.label),
                **({"metric": c.metric} if c.metric else {}),
                **({"format": c.format} if c.format else {}),
                **({"unit": c.unit} if c.unit else {}),
            }
            for c in full.columns.values()
        ],
        "rows": [
            {
                "label": row.label,
                "kind": row.kind,
                **({"small": True} if row.small else {}),
                **({"n": row.n} if row.n is not None else {}),
                "cells": {
                    col: _cell(cell, short.rows[i].cells.get(col))
                    for col, cell in row.cells.items()
                },
            }
            for i, row in enumerate(full.rows)
        ],
    }


def _raw_cell(value: Any) -> dict:
    """A value of a Python source as it is; a missing value is null with an empty text."""
    value = plain(value)
    return {"value": value, "text": "" if value is None else str(value)}


def _raw(result: Result) -> dict:
    """A result of a Python source: its columns and values as they are."""
    return {
        "columns": [
            {"id": str(c), "kind": "other", "label": str(c).replace("_", " ").capitalize()}
            for c in result.df.columns
        ],
        "rows": [
            {
                "label": "",
                "kind": "row",
                "cells": {str(c): _raw_cell(v) for c, v in row.items()},
            }
            for row in result.df.to_dict("records")
        ],
    }  # fmt: skip


def result_json(result: Result) -> dict:
    """A result with its display cells and its metadata."""
    meta = result.meta
    body: dict[str, Any] = {"shape": meta.shape, "empty": meta.empty}
    if meta.window is None:
        return {**body, **_raw(result)}
    window = meta.window
    body["window"] = {
        "key": window.key,
        "current": _window(window.current),
        "compare": window.compare,
        "compare_ok": window.compare_ok,
        "compare_label": window.compare_label,
        "comparison": _window(window.comparison),
    }
    body["trading_days"] = meta.trading_days
    body["prior_trading_days"] = meta.prior_trading_days
    if meta.grain:
        body["grain"] = meta.grain
    body["dimensions"] = {d: dim.label for d, dim in meta.dimensions.items()}
    body["metrics"] = {
        m: {
            "label": spec.label,
            "description": spec.description,
            "format": spec.format,
            "unit": formats.unit(spec.format),
            "up_is_good": spec.up_is_good,
        }
        for m, spec in meta.value_metrics.items()
    }
    if meta.events:
        body["events"] = [{"date": e.date, "kind": e.kind, "name": e.name} for e in meta.events]
    if meta.buckets:
        body["buckets"] = [
            {"ordinal": b.ordinal, "start": b.start, "end": b.end} for b in meta.buckets
        ]
    if meta.decomposition is not None:
        d = meta.decomposition
        body["decomposition"] = {"identity": d.identity, "kind": d.kind, "kpi": d.kpi}
    return {**body, **_presented(result)}


def _facets(facets: Sequence[Facet]) -> tuple[list[dict], list[dict]]:
    """The distinct results of a slot and its facets, which point at them (facets by metric
    share one result)."""
    results: list[dict] = []
    index: dict[int, int] = {}
    out = []
    for f in facets:
        if id(f.result) not in index:
            index[id(f.result)] = len(results)
            results.append(result_json(f.result))
        out.append(
            {
                "key": plain(f.key) if not isinstance(f.key, str | None) else f.key,
                "label": f.label,
                "metric": f.metric,
                "result": index[id(f.result)],
            }
        )
    return results, out


def _insight(i: Insight) -> dict:
    return {
        "text": i.text,
        "tone": i.tone,
        "rule": i.rule,
        "score": round(i.score, 4),
        "component": i.component,
        "facet": i.facet,
    }


def _slot(run: SlotRun) -> dict:
    slot = run.slot
    spec = slot.bound.spec
    results, facets = _facets(run.facets)
    return {
        "component": spec.id,
        "title": run.title,
        "hint": run.hint,
        "note": run.note,
        "visual": {"type": slot.visual, "options": spec.visual.options if spec.visual else {}},
        "layout": spec.facet.layout if spec.facet else "row",
        "position": {
            "row": slot.spec.row,
            "col": slot.spec.col,
            "span": slot.spec.span,
            "height": slot.spec.height,
        },
        "card": slot.card,
        "filters": [
            {"kind": f.kind, "dimension": f.dimension, "origin": f.origin, "value": f.value}
            for f in slot.ref.filters
        ],
        "results": results,
        "facets": facets,
        "insights": [_insight(i) for i in run.insights],
        "insights_status": run.status,
    }


def _page(run: PageRun) -> dict:
    page = run.page
    return {
        "id": page.id,
        "base": page.base,
        "title": page.title,
        "params": page.params,
        "period": page.period,
        "chips": [{"text": t, "style": s} for t, s in run.chips],
        "note": run.note,
        "empty_message": page.empty_message,
        "grid": {"columns": page.grid.columns, "rows": page.grid.rows},
        "slots": [_slot(s) for s in run.slots],
        "insights": [_insight(i) for i in run.insights],
    }


def _theme(theme: Theme) -> dict:
    """The colours a renderer needs to draw the tokens of the visuals' options."""
    return {
        "font": theme.font,
        "colors": {token: theme.color(token) for token in TOKENS},
        "series": list(theme.palette.series),
    }


def bundle(run: ReportRun, theme: Theme = DEFAULT_THEME) -> dict:
    """The bundle of a report run as plain Python values."""
    report = run.report.spec
    return {
        "format_version": FORMAT_VERSION,
        "report": {
            "id": report.id,
            "title": report.title,
            "kicker": run.report.kicker,
            "footer": run.footer,
            "anchor": run.anchor,
            "generated": run.generated,
            "run": run.ctx.run.name if run.ctx.run is not None else None,
            "params": run.report.params,
            "insights": report.insights,
        },
        "theme": _theme(theme),
        "pages": [_page(p) for p in run.pages],
        "texts": run.texts,
    }


def _clean(value: Any) -> Any:
    """Plain JSON: dates as ISO text, NaN as null, numpy values as Python values."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, pd.Timestamp | datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if hasattr(value, "item") and not isinstance(value, str):
        return _clean(value.item())
    return value


def to_json(run: ReportRun, theme: Theme = DEFAULT_THEME) -> str:
    text = json.dumps(_clean(bundle(run, theme)), indent=1, ensure_ascii=False, allow_nan=False)
    return text + "\n"


def write_bundle(path: Path, run: ReportRun, theme: Theme = DEFAULT_THEME) -> Path:
    path.write_text(to_json(run, theme))
    return path
