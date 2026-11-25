"""The semantic manifest (plan 8.10): the catalogue of the semantic layer in one JSON file.

`hari-lari-component manifest` writes `semantic_manifest.json`, like dbt's `manifest.json`: what
can be asked and what is already asked. It lists

- the **models** (`models.yml`), with the metrics and dimensions that exist on each;
- the **metrics** with label, description, format, unit, kind, additivity and sample-size rule,
  and what each depends on;
- the **dimensions** with their grain, models, labels and bins;
- the **KPI tree** (`kpi_tree.yml`): the identities `decompose` and the `drivers` rule use;
- the **window** forms and the **transforms**, **visuals** and **insight rules** with their
  option names;
- the **components** and **reports** with their parameters, queries and pages.

A frontend reads labels and descriptions from it instead of repeating them; the Cursor rule
`.cursor/rules/reporting-components.mdc` points an LLM at it, so a new component starts from
what exists. The file is written to `reporting/semantic_manifest.json` and a test fails when it
is out of date, like the JSON Schemas. It holds no data and no business identifiers: only the
definitions of the packaged YAML files and the few project settings that decide how a number
is written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from hari_lari_analytics.project import project
from hari_lari_analytics.reporting import formats, insights, transforms, visuals
from hari_lari_analytics.reporting.components import Component, load_components
from hari_lari_analytics.reporting.registry import Registry, default_registry
from hari_lari_analytics.reporting.reports import Report, load_reports
from hari_lari_analytics.reporting.transforms import parse_step

FORMAT_VERSION = 1

# The forms of a window name, each with an example that `parse_window` accepts (a test checks it).
WINDOW_FORMS: list[dict[str, str]] = [
    {
        "form": "L<n>D",
        "example": "L14D",
        "description": "the last n days, ending on the anchor; also last_n_days(n)",
    },
    {
        "form": "L<n>W",
        "example": "L6W",
        "description": "the last n complete weeks, ending on the last day of the week of the "
        "anchor (the first weekday is a project setting); also last_n_weeks(n)",
    },
    {
        "form": "L<n>M",
        "example": "L3M",
        "description": "the last n complete months; also last_n_months(n)",
    },
    {"form": "WTD", "example": "WTD", "description": "the week to date"},
    {"form": "MTD", "example": "MTD", "description": "the month to date"},
    {"form": "full", "example": "full", "description": "the whole history, from the first sale"},
]
COMPARES = {
    "prior": "the period of the same length right before the window",
    "last_year": "the same dates a year earlier",
    "none": "no comparison",
}


def manifest_path() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "semantic_manifest.json"))


# --- the parts ------------------------------------------------------------------------------------


def _project() -> dict:
    p = project()
    return {
        "currency": {"code": p.currency.code, "decimals": p.currency.decimals},
        "timezone": p.timezone,
        "first_weekday": p.first_weekday,
        "min_n": p.min_n,
    }


def _models(registry: Registry) -> list[dict]:
    return [
        {
            "id": spec.id,
            "description": spec.description,
            "primary_key": spec.primary_key,
            "time": spec.time,
            "calendar": spec.calendar,
            "sample_size": spec.sample_size,
            "exposed": spec.exposed,
            "metrics": registry.metrics_on(spec.id) if spec.exposed else [],
            "dimensions": [d.id for d in registry.dimensions.values() if spec.id in d.models],
        }
        for spec in registry.models.values()
    ]


def _metrics(registry: Registry) -> list[dict]:
    out = []
    for metric_id in registry.metrics:
        m = registry.metric(metric_id)
        entry: dict[str, Any] = {
            "id": m.id,
            "kind": m.kind,
            "label": m.label,
            "description": m.description,
            "format": m.format,
            "unit": formats.unit(m.format),
            "up_is_good": m.up_is_good,
            "additivity": m.additivity,
            "min_n": m.min_n,  # null: the default rule of the model
            "models": sorted(m.models),
            "depends_on": sorted(m.depends_on),
        }
        if m.expression is not None:
            entry["expression"] = m.expression
        if m.aggregates:
            entry["aggregates"] = {model: a.sql for model, a in sorted(m.aggregates.items())}
        out.append(entry)
    return out


def _dimensions(registry: Registry) -> list[dict]:
    out = []
    for d in registry.dimensions.values():
        entry: dict[str, Any] = {
            "id": d.id,
            "label": d.label,
            "description": d.description,
            "grain": d.grain,
            "models": d.models,
            "domain": d.domain,
            "sort": {"by": d.sort.by, "desc": d.sort.desc},
        }
        if d.values is not None:
            entry["values"] = d.values
        if d.labels:
            entry["labels"] = {str(k): v for k, v in d.labels.items()}
        if d.null_label:
            entry["null_label"] = d.null_label
        if d.bins:
            entry["bins"] = [b.label for b in d.bin_list]
        out.append(entry)
    return out


def _kpi_tree(registry: Registry) -> list[dict]:
    return [
        i.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
        for i in registry.identities.values()
    ]


def _doc(fn: Any) -> str:
    """The first paragraph of a docstring on one line; the registries keep only its first line."""
    paragraph = (fn.__doc__ or "").strip().split("\n\n")[0]
    return " ".join(paragraph.split())


def _options(model: type) -> list[str]:
    return sorted(model.model_fields)  # type: ignore[attr-defined]


def _transforms() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": _doc(t.fn),
            "options": _options(t.options),
            "shapes": list(t.shapes),
            "produces": t.produces,
        }
        for t in sorted(transforms.TRANSFORMS.values(), key=lambda t: t.name)
    ]


def _visuals() -> list[dict]:
    return [
        {
            "name": v.name,
            "description": _doc(v.fn),
            "options": _options(v.options),
            "accepts": list(v.accepts),
            "min_size": list(v.min_size),
        }
        for v in sorted(visuals.VISUALS.values(), key=lambda v: v.name)
    ]


def _insight_rules() -> list[dict]:
    return [
        {
            "name": r.name,
            "type": r.type.name,
            "description": _doc(r.type.fn),
            "options": r.options,
            "weight": r.weight,
            "compares": r.type.compares,
        }
        for r in insights.default_rules().values()
    ]


def _component(c: Component) -> dict:
    raw = c.raw
    query = raw["query"]
    entry: dict[str, Any] = {
        "id": c.id,
        "title": raw["title"],
        "description": raw.get("description"),
        "hint": raw.get("hint"),
        "note": raw.get("note"),
        "params": dict(c.defaults),
        "unbound": sorted(c.unbound),
        "query": query,
        "transforms": [parse_step(step)[0] for step in raw.get("transforms", [])],
        "facet": raw.get("facet"),
        "visual": (raw.get("visual") or {}).get("type"),
        "insights": [
            next(iter(rule)) if isinstance(rule, Mapping) else rule
            for rule in raw.get("insights", [])
        ],
    }
    return entry


def _report(report: Report) -> dict:
    spec = report.spec
    return {
        "id": spec.id,
        "title": spec.title,
        "description": spec.description,
        "params": spec.params,
        "schedule": spec.schedule.model_dump(mode="json") if spec.schedule else None,
        "outputs": spec.outputs.model_dump(mode="json", by_alias=True),
        "pages": [
            {
                "id": p.id,
                "title": p.title,
                "components": sorted({s.component for s in p.slots}),
            }
            for p in spec.plain_pages()
        ],
    }


# --- the manifest ---------------------------------------------------------------------------------


def build_manifest(
    components: Mapping[str, Component] | None = None,
    reports: Mapping[str, Report] | None = None,
    registry: Registry | None = None,
) -> dict:
    """The manifest as plain values. Without arguments: the packaged registries, components and
    reports."""
    registry = registry or default_registry()
    components = load_components() if components is None else components
    reports = load_reports(components) if reports is None else reports
    return {
        "format_version": FORMAT_VERSION,
        "project": _project(),
        "models": _models(registry),
        "metrics": _metrics(registry),
        "default_min_n": registry.default_min_n,
        "dimensions": _dimensions(registry),
        "kpi_tree": _kpi_tree(registry),
        "windows": {"forms": WINDOW_FORMS, "compare": COMPARES},
        "transforms": _transforms(),
        "visuals": _visuals(),
        "insight_rules": _insight_rules(),
        "components": [_component(components[i]) for i in sorted(components)],
        "reports": [_report(reports[i]) for i in sorted(reports)],
    }


def manifest_text(
    components: Mapping[str, Component] | None = None,
    reports: Mapping[str, Report] | None = None,
    registry: Registry | None = None,
) -> str:
    raw = build_manifest(components, reports, registry)
    return json.dumps(raw, indent=2, ensure_ascii=False, default=str) + "\n"


def write_manifest(path: Path | None = None, text: str | None = None) -> Path:
    path = path or manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest_text() if text is None else text)
    return path
