"""Snapshots: the golden result of a component on the test fixture, as one YAML file.

`tests/snapshots/<id>.yml` holds the parameters the component is run with and, per facet, its
key, label and result as CSV (a result that facets by metric share is written once), so a
change to the compiler, a transform or a metric shows in the diff exactly which components it
moves. `hari-lari-component new` writes a stub with only the
parameters; `uv run pytest tests/test_snapshots.py --update-snapshots` fills in the results.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import yaml

from hari_lari_analytics.presentation.facets import Facet

HEADER = (
    "# The result of the component on the test fixture (tests/test_snapshots.py).\n"
    "# Refresh with `uv run pytest tests/test_snapshots.py --update-snapshots` and read the diff.\n"
)


class _Dumper(yaml.SafeDumper):
    pass


def _text(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _text)


def _plain(value: Any) -> Any:
    """A facet key as a YAML scalar: dates and numpy values become plain values."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def result_csv(facet: Facet) -> str:
    return facet.result.df.to_csv(index=False, float_format="%.10g", lineterminator="\n")


def snapshot_text(
    component_id: str, params: Mapping[str, Any], facets: Sequence[Facet] | None = None
) -> str:
    """The snapshot file; without `facets` a stub that names only the parameters."""
    doc: dict[str, Any] = {"component": component_id, "params": dict(params)}
    if facets is not None:
        doc["facets"] = []
        first: dict[int, Any] = {}  # facets by metric share one result: written once
        for f in facets:
            entry = {"key": _plain(f.key), "label": f.label, "metric": f.metric}
            if id(f.result) in first:
                entry["same_result_as"] = first[id(f.result)]
            else:
                first[id(f.result)] = entry["key"]
                entry.update(shape=f.result.meta.shape, csv=result_csv(f))
            doc["facets"].append(entry)
    body = yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)
    return HEADER + body


def read_snapshot(text: str) -> dict[str, Any]:
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ValueError("a snapshot is a mapping with component, params and facets")
    return doc
