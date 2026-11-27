"""The workbook: one formatted sheet per listed component, and the lines export.

A component's sheet holds its result in the output convention (dimension columns, metrics,
`prior_*`, `*_change`, `*_delta`, the sample sizes), as numbers that Excel can compute with,
under the labels of the registry and with a number format per metric format. A component with
facets by dimension or period writes them under one another with a `Facet` column first. The
`lines_export` source writes the lines as a filterable table, ready for pivoting, and a column
guide, without personal data and without the line note.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from hari_lari_analytics.reporting import formats
from hari_lari_analytics.reporting.facets import Facet
from hari_lari_analytics.reporting.queries import SourceQuery
from hari_lari_analytics.reporting.queries.lines_export import COLUMNS
from hari_lari_analytics.reporting.result import Result

FORMATS = {
    "idr": "#,##0",
    "idr_compact": "#,##0",
    "count": "#,##0",
    "decimal": "#,##0.00",
    "pct": "0.0%",
    "timestamp": "yyyy-mm-dd hh:mm",
}
RATIO = "+0.0%;-0.0%;0.0%"
POINTS = '+0.0" pp";-0.0" pp";0.0" pp"'
DAY = "yyyy-mm-dd"
_BAD_SHEET_NAME = re.compile(r"[\[\]:*?/\\]")
MAX_SHEET_NAME = 31


def sheet_name(wanted: str, taken: Sequence[str]) -> str:
    """A valid, unique sheet name (at most 31 characters, none of []:*?/\\)."""
    base = _BAD_SHEET_NAME.sub(" ", wanted).strip()[:MAX_SHEET_NAME] or "Sheet"
    name, n = base, 1
    while name.lower() in (t.lower() for t in taken):
        n += 1
        tail = f" {n}"
        name = base[: MAX_SHEET_NAME - len(tail)] + tail
    return name


def _label(result: Result, column: str) -> str:
    meta = result.meta
    metrics = meta.value_metrics
    if column in meta.dimensions:
        return meta.dimensions[column].label
    if column in metrics:
        spec = metrics[column]
        unit = formats.unit(spec.format)
        return f"{spec.label} ({unit})" if unit else spec.label
    for prefix, suffix, text in (
        ("prior_", "", "Prior {}"),
        ("", "_change", "{} change"),
        ("", "_delta", "{} delta"),
    ):
        if column.startswith(prefix) and column.endswith(suffix):
            inner = column[len(prefix) : len(column) - len(suffix)]
            if inner in metrics:
                if suffix == "_delta" and metrics[inner].format == "pct":
                    return f"{metrics[inner].label} delta (pp)"
                return text.format(metrics[inner].label)
    if column in meta.n_columns:
        return f"{meta.n_columns[column].label} (n)"
    return column.replace("_", " ").strip().capitalize()


def _number_format(result: Result, column: str, series: pd.Series) -> str | None:
    metrics = result.meta.value_metrics
    if column in metrics:
        return FORMATS.get(metrics[column].format)
    for prefix, suffix in (("prior_", ""), ("", "_change"), ("", "_delta")):
        if column.startswith(prefix) and column.endswith(suffix):
            inner = column[len(prefix) : len(column) - len(suffix)]
            if inner in metrics:
                spec = metrics[inner]
                if suffix == "_change":
                    return RATIO
                if suffix == "_delta":
                    return POINTS if spec.format == "pct" else "+#,##0;-#,##0;0"
                return FORMATS.get(spec.format)
    if pd.api.types.is_datetime64_any_dtype(series):
        return DAY
    if column in result.meta.n_columns or column in ("trading_days", "prior_trading_days"):
        return "#,##0"
    return None


def sheet_frame(facets: Sequence[Facet]) -> tuple[pd.DataFrame, Result]:
    """The rows of a component's facets as one table (a `Facet` column first when they differ),
    with the result whose labels and formats name the columns."""
    unique: dict[int, Facet] = {}
    for f in facets:
        unique.setdefault(id(f.result), f)
    first = facets[0].result
    if len(unique) == 1:
        return first.df.copy(), first
    frames = [f.result.df.assign(facet=f.label or str(f.key)) for f in unique.values()]
    df = pd.concat(frames, ignore_index=True)
    return df[["facet", *[c for c in df.columns if c != "facet"]]], first


def _delta_points(df: pd.DataFrame, result: Result) -> pd.DataFrame:
    """The deltas of `pct` metrics in percentage points (the result holds fractions)."""
    df = df.copy()
    for column, spec in result.meta.value_metrics.items():
        if spec.format == "pct" and f"{column}_delta" in df:
            df[f"{column}_delta"] = df[f"{column}_delta"] * 100
    return df


def write_component_sheet(writer: pd.ExcelWriter, name: str, facets: Sequence[Facet]) -> None:
    df, result = sheet_frame(facets)
    has_meta = result.meta.window is not None
    if has_meta:
        df = _delta_points(df, result)
    headers = {c: (_label(result, c) if has_meta else c.replace("_", " ").capitalize()) for c in df}
    if "facet" in df:
        headers["facet"] = "Facet"
    out = df.rename(columns=headers)
    out.to_excel(writer, sheet_name=name, index=False)
    ws = writer.sheets[name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for index, column in enumerate(df.columns, start=1):
        letter = get_column_letter(index)
        header = headers[column]
        width = max([len(header), *(len(str(v)) for v in out.iloc[:200, index - 1])])
        ws.column_dimensions[letter].width = min(max(width + 3, 10), 60)
        fmt = _number_format(result, column, df[column]) if has_meta else None
        if fmt is None and pd.api.types.is_datetime64_any_dtype(df[column]):
            fmt = DAY
        if fmt:
            for (cell,) in ws.iter_rows(min_row=2, min_col=index, max_col=index):
                cell.number_format = fmt


PIVOT_TIPS = [
    "Gross / Discounts / Refunds / Net Sales IDR, Items Sold (= Quantity): use Sum.",
    "Orders: tick 'Add this data to the Data Model' when inserting the PivotTable, filter "
    "'Counts As Order' = TRUE, then use 'Distinct Count' of Receipt Nr. Never sum orders "
    "across categories or items: one receipt can contain several.",
    "Average Order Value = Net Sales IDR / Orders.",
    "% Category Order Penetration = orders containing the category / all orders in the same "
    "filter. It is easiest to read in the PDF report.",
    "Category AOV = net sales of the category / orders containing the category. "
    "Contributive AOV = penetration × Category AOV = net sales of the category / all orders; "
    "it adds up to the Average Order Value over all categories.",
]


def write_lines_sheets(writer: pd.ExcelWriter, lines: pd.DataFrame) -> None:
    """The lines table and its column guide as two sheets of an open workbook (openpyxl)."""
    columns = [c for c, *_ in COLUMNS]
    data = lines[columns].rename(columns={c: h for c, h, *_ in COLUMNS})
    data = data.sort_values(["Sale Timestamp", "Receipt Nr"]).reset_index(drop=True)

    data.to_excel(writer, sheet_name="Sales Lines", index=False)
    ws = writer.sheets["Sales Lines"]
    last = f"{get_column_letter(len(COLUMNS))}{len(data) + 1}"
    table = Table(displayName="SalesLines", ref=f"A1:{last}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium7", showRowStripes=True)
    ws.add_table(table)
    ws.freeze_panes = "B2"
    for index, (_, header, number_format, _) in enumerate(COLUMNS, start=1):
        letter = get_column_letter(index)
        ws.column_dimensions[letter].width = max(12, len(header) + 4)
        if number_format:
            for (cell,) in ws.iter_rows(min_row=2, min_col=index, max_col=index):
                cell.number_format = number_format

    guide = pd.DataFrame([(h, d) for _, h, _, d in COLUMNS], columns=["Column", "Description"])
    guide.to_excel(writer, sheet_name="Column Guide", index=False)
    tips = pd.DataFrame({"How to pivot": PIVOT_TIPS})
    tips.to_excel(writer, sheet_name="Column Guide", index=False, startrow=len(guide) + 2)
    guide_ws = writer.sheets["Column Guide"]
    guide_ws.column_dimensions["A"].width = 28
    guide_ws.column_dimensions["B"].width = 90


def is_lines_export(query: object) -> bool:
    return isinstance(query, SourceQuery) and query.source == "lines_export"


def write_workbook(
    path: Path, sheets: Sequence[tuple[str, object, Sequence[Facet]]]
) -> Path | None:
    """Write the workbook: `sheets` lists (wanted sheet name, the component's query, facets).
    A component whose window has no sales has no facets and no sheet; without any sheet there
    is no file (None)."""
    sheets = [s for s in sheets if s[2]]
    if not sheets:
        return None
    taken: list[str] = []
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for wanted, query, facets in sheets:
            if is_lines_export(query):
                write_lines_sheets(writer, facets[0].result.df)
                taken += ["Sales Lines", "Column Guide"]
                continue
            name = sheet_name(wanted, taken)
            taken.append(name)
            write_component_sheet(writer, name, facets)
    return path
