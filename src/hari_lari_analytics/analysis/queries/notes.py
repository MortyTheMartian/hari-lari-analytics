"""Data notes: where the data comes from, extraction warnings and data-check warnings."""

from __future__ import annotations

import pandas as pd

from hari_lari_analytics.analysis.queries import NoOptions, QueryContext, query
from hari_lari_analytics.analysis.result import Result, ResultMeta


@query("notes")
def notes(ctx: QueryContext, options: NoOptions) -> Result:
    """One row per note, with its `kind`: source, extraction or check."""
    rows: list[tuple[str, str]] = []
    if ctx.run is not None:
        rows.append(("source", f"Moka API extraction {ctx.run.name}"))
        manifest = ctx.run.manifest
        rows += [("extraction", f"Extraction error: {e}") for e in manifest.get("errors", [])]
        rows += [("extraction", f"Extraction warning: {w}") for w in manifest.get("warnings", [])]
    if ctx.checks is not None:
        rows += [("check", result.describe()) for result in ctx.checks.warnings]
    df = pd.DataFrame(rows, columns=["kind", "note"])
    return Result(df, ResultMeta(shape="rows", window=None, empty=df.empty))
